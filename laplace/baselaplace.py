import warnings
from collections.abc import MutableMapping
from math import log, pi, sqrt

import numpy as np
import torch
import torchmetrics as tm
import tqdm
from torch.distributions import MultivariateNormal
from torch.nn.utils import parameters_to_vector, vector_to_parameters
from torch.utils.data import DataLoader
from torchmetrics import MeanSquaredError

from laplace.curvature import AsdlGGN, BackPackGGN, CurvlinopsGGN
from laplace.curvature.asdfghjkl import AsdfghjklHessian
from laplace.curvature.curvlinops import CurvlinopsEF
from laplace.utils import (Kron, RunningNLLMetric, SoDSampler,
                           fix_prior_prec_structure, invsqrt_precision,
                           normal_samples, validate)

__all__ = [
    'BaseLaplace',
    'ParametricLaplace',
    'FunctionalLaplace',
    'FullLaplace',
    'KronLaplace',
    'DiagLaplace',
    'LowRankLaplace',
]


class BaseLaplace:
    """Baseclass for all Laplace approximations in this library.

    Parameters
    ----------
    model : torch.nn.Module
    likelihood : {'classification', 'regression', 'reward_modeling'}
        determines the log likelihood Hessian approximation.
        In the case of 'reward_modeling', it fits Laplace in using the classification likelihood,
        then do prediction as in regression likelihood. The model needs to be defined accordingly:
        The forward pass during training takes `x.shape == (batch_size, 2, dim)` with
        `y.shape = (batch_size,)`. Meanwhile, during evaluation `x.shape == (batch_size, dim)`.
        Note that 'reward_modeling' only supports `KronLaplace` and `DiagLaplace`.
    sigma_noise : torch.Tensor or float, default=1
        observation noise for the regression setting; must be 1 for classification
    prior_precision : torch.Tensor or float, default=1
        prior precision of a Gaussian prior (= weight decay);
        can be scalar, per-layer, or diagonal in the most general case
    prior_mean : torch.Tensor or float, default=0
        prior mean of a Gaussian prior, useful for continual learning
    temperature : float, default=1
        temperature of the likelihood; lower temperature leads to more
        concentrated posterior and vice versa.
    enable_backprop: bool, default=False
        whether to enable backprop to the input `x` through the Laplace predictive.
        Useful for e.g. Bayesian optimization.
    backend : subclasses of `laplace.curvature.CurvatureInterface`
        backend for access to curvature/Hessian approximations. Defaults to CurvlinopsGGN if None.
    backend_kwargs : dict, default=None
        arguments passed to the backend on initialization, for example to
        set the number of MC samples for stochastic approximations.
    asdl_fisher_kwargs : dict, default=None
        arguments passed to the ASDL backend specifically on initialization.
    """

    def __init__(
        self,
        model,
        likelihood,
        sigma_noise=1.0,
        prior_precision=1.0,
        prior_mean=0.0,
        temperature=1.0,
        enable_backprop=False,
        backend=None,
        backend_kwargs=None,
        asdl_fisher_kwargs=None,
    ):
        if likelihood not in ['classification', 'regression', 'reward_modeling']:
            raise ValueError(f'Invalid likelihood type {likelihood}')

        self.model = model

        # Only do Laplace on params that require grad
        self.params = []
        self.is_subset_params = False
        for p in model.parameters():
            if p.requires_grad:
                self.params.append(p)
            else:
                self.is_subset_params = True

        self.n_params = sum(p.numel() for p in self.params)
        self.n_layers = len(self.params)
        self.prior_precision = prior_precision
        self.prior_mean = prior_mean
        if sigma_noise != 1 and likelihood != 'regression':
            raise ValueError('Sigma noise != 1 only available for regression.')

        self.reward_modeling = likelihood == 'reward_modeling'
        if self.reward_modeling:
            # For fitting only. After it's done, self.likelihood = 'regression', see self.fit()
            self.likelihood = 'classification'
        else:
            self.likelihood = likelihood

        self.sigma_noise = sigma_noise
        self.temperature = temperature
        self.enable_backprop = enable_backprop

        if backend is None:
            backend = CurvlinopsGGN
        else:
            if self.is_subset_params and 'backpack' in backend.__name__.lower():
                raise ValueError(
                    'If some grad are switched off, the BackPACK backend is not supported.'
                )

        self._backend = None
        self._backend_cls = backend
        self._backend_kwargs = dict() if backend_kwargs is None else backend_kwargs
        self._asdl_fisher_kwargs = (
            dict() if asdl_fisher_kwargs is None else asdl_fisher_kwargs
        )

        # log likelihood = g(loss)
        self.loss = 0.0
        self.n_outputs = None
        self.n_data = 0

    @property
    def _device(self):
        return next(self.model.parameters()).device

    @property
    def backend(self):
        if self._backend is None:
            self._backend = self._backend_cls(
                self.model, self.likelihood, **self._backend_kwargs
            )
        return self._backend

    def _curv_closure(self, X, y, N):
        raise NotImplementedError

    def fit(self, train_loader):
        raise NotImplementedError

    def log_marginal_likelihood(self, prior_precision=None, sigma_noise=None):
        raise NotImplementedError

    @property
    def log_likelihood(self):
        """Compute log likelihood on the training data after `.fit()` has been called.
        The log likelihood is computed on-demand based on the loss and, for example,
        the observation noise which makes it differentiable in the latter for
        iterative updates.

        Returns
        -------
        log_likelihood : torch.Tensor
        """
        factor = -self._H_factor
        if self.likelihood == 'regression':
            # loss used is just MSE, need to add normalizer for gaussian likelihood
            c = (
                self.n_data
                * self.n_outputs
                * torch.log(self.sigma_noise * sqrt(2 * pi))
            )
            return factor * self.loss - c
        else:
            # for classification Xent == log Cat
            return factor * self.loss

    def __call__(self, x, pred_type, link_approx, n_samples):
        raise NotImplementedError

    def predictive(self, x, pred_type, link_approx, n_samples):
        return self(x, pred_type, link_approx, n_samples)

    def _check_jacobians(self, Js):
        if not isinstance(Js, torch.Tensor):
            raise ValueError('Jacobians have to be torch.Tensor.')
        if not Js.device == self._device:
            raise ValueError('Jacobians need to be on the same device as Laplace.')
        m, k, p = Js.size()
        if p != self.n_params:
            raise ValueError('Invalid Jacobians shape for Laplace posterior approx.')

    @property
    def prior_precision_diag(self):
        """Obtain the diagonal prior precision \\(p_0\\) constructed from either
        a scalar, layer-wise, or diagonal prior precision.

        Returns
        -------
        prior_precision_diag : torch.Tensor
        """
        if len(self.prior_precision) == 1:  # scalar
            return self.prior_precision * torch.ones(self.n_params, device=self._device)

        elif len(self.prior_precision) == self.n_params:  # diagonal
            return self.prior_precision

        elif len(self.prior_precision) == self.n_layers:  # per layer
            n_params_per_layer = [p.numel() for p in self.params]
            return torch.cat(
                [
                    prior * torch.ones(n_params, device=self._device)
                    for prior, n_params in zip(self.prior_precision, n_params_per_layer)
                ]
            )

        else:
            raise ValueError(
                'Mismatch of prior and model. Diagonal, scalar, or per-layer prior.'
            )

    @property
    def prior_mean(self):
        return self._prior_mean

    @prior_mean.setter
    def prior_mean(self, prior_mean):
        if np.isscalar(prior_mean) and np.isreal(prior_mean):
            self._prior_mean = torch.tensor(prior_mean, device=self._device)
        elif torch.is_tensor(prior_mean):
            if prior_mean.ndim == 0:
                self._prior_mean = prior_mean.reshape(-1).to(self._device)
            elif prior_mean.ndim == 1:
                if len(prior_mean) not in [1, self.n_params]:
                    raise ValueError('Invalid length of prior mean.')
                self._prior_mean = prior_mean
            else:
                raise ValueError('Prior mean has too many dimensions!')
        else:
            raise ValueError('Invalid argument type of prior mean.')

    @property
    def prior_precision(self):
        return self._prior_precision

    @prior_precision.setter
    def prior_precision(self, prior_precision):
        self._posterior_scale = None
        if np.isscalar(prior_precision) and np.isreal(prior_precision):
            self._prior_precision = torch.tensor([prior_precision], device=self._device)
        elif torch.is_tensor(prior_precision):
            if prior_precision.ndim == 0:
                # make dimensional
                self._prior_precision = prior_precision.reshape(-1).to(self._device)
            elif prior_precision.ndim == 1:
                if len(prior_precision) not in [1, self.n_layers, self.n_params]:
                    raise ValueError(
                        'Length of prior precision does not align with architecture.'
                    )
                self._prior_precision = prior_precision.to(self._device)
            else:
                raise ValueError(
                    'Prior precision needs to be at most one-dimensional tensor.'
                )
        else:
            raise ValueError(
                'Prior precision either scalar or torch.Tensor up to 1-dim.'
            )

    def optimize_prior_precision_base(
        self,
        pred_type,
        method='marglik',
        n_steps=100,
        lr=1e-1,
        init_prior_prec=1.0,
        prior_structure='scalar',
        val_loader=None,
        loss=None,
        log_prior_prec_min=-4,
        log_prior_prec_max=4,
        grid_size=100,
        link_approx='probit',
        n_samples=100,
        verbose=False,
        cv_loss_with_var=False,
        progress_bar=False,
    ):
        """Optimize the prior precision post-hoc using the `method`
        specified by the user.

        Parameters
        ----------
        pred_type : {'glm', 'nn', 'gp'}, default='glm'
            type of posterior predictive, linearized GLM predictive or neural
            network sampling predictive or Gaussian Process (GP) inference.
            The GLM predictive is consistent with the curvature approximations used here.
        method : {'marglik', 'gridsearch'}, default='marglik'
            specifies how the prior precision should be optimized.
        n_steps : int, default=100
            the number of gradient descent steps to take.
        lr : float, default=1e-1
            the learning rate to use for gradient descent.
        init_prior_prec : float or tensor, default=1.0
            initial prior precision before the first optimization step.
        prior_structure : {'scalar', 'layerwise', 'diag'}, default='scalar'
            if init_prior_prec is scalar, the prior precision is optimized with this structure.
            otherwise, the structure of init_prior_prec is maintained.
        val_loader : torch.data.utils.DataLoader, default=None
            DataLoader for the validation set; each iterate is a training batch (X, y).
        loss : callable or torchmetrics.Metric, default=None
            loss function to use for CV. If callable, the loss is computed offline (memory intensive).
            If torchmetrics.Metric, running loss is computed (efficient). The default
            depends on the likelihood: `RunningNLLMetric()` for classification and
            reward modeling, running `MeanSquaredError()` for regression.
        cv_loss_with_var: bool, default=False
            if true, `loss` takes three arguments `loss(output_mean, output_var, target)`,
            otherwise, `loss` takes two arguments `loss(output_mean, target)`
        log_prior_prec_min : float, default=-4
            lower bound of gridsearch interval.
        log_prior_prec_max : float, default=4
            upper bound of gridsearch interval.
        grid_size : int, default=100
            number of values to consider inside the gridsearch interval.
        link_approx : {'mc', 'probit', 'bridge'}, default='probit'
            how to approximate the classification link function for the `'glm'`.
            For `pred_type='nn'`, only `'mc'` is possible.
        n_samples : int, default=100
            number of samples for `link_approx='mc'`.
        verbose : bool, default=False
            if true, the optimized prior precision will be printed
            (can be a large tensor if the prior has a diagonal covariance).
        progress_bar : bool, default=False
            whether to show a progress bar; updated at every batch-Hessian computation.
            Useful for very large model and large amount of data, esp. when `subset_of_weights='all'`.
        """
        if method == 'marglik':
            self.prior_precision = init_prior_prec
            if len(self.prior_precision) == 1 and prior_structure != 'scalar':
                self.prior_precision = fix_prior_prec_structure(
                    self.prior_precision.item(),
                    prior_structure,
                    self.n_layers,
                    self.n_params,
                    self._device,
                )
            log_prior_prec = self.prior_precision.log()
            log_prior_prec.requires_grad = True
            optimizer = torch.optim.Adam([log_prior_prec], lr=lr)

            if progress_bar:
                pbar = tqdm.trange(n_steps)
                pbar.set_description('[Optimizing marginal likelihood]')
            else:
                pbar = range(n_steps)

            for _ in pbar:
                optimizer.zero_grad()
                prior_prec = log_prior_prec.exp()
                neg_log_marglik = -self.log_marginal_likelihood(
                    prior_precision=prior_prec
                )
                neg_log_marglik.backward()
                optimizer.step()
            self.prior_precision = log_prior_prec.detach().exp()
        elif method == 'gridsearch':
            if val_loader is None:
                raise ValueError('gridsearch requires a validation set DataLoader')

            interval = torch.logspace(log_prior_prec_min, log_prior_prec_max, grid_size)

            if loss is None:
                loss = (
                    MeanSquaredError(num_outputs=self.n_outputs)
                    if self.likelihood == 'regression'
                    else RunningNLLMetric()
                )

            self.prior_precision = self._gridsearch(
                loss,
                interval,
                val_loader,
                pred_type=pred_type,
                link_approx=link_approx,
                n_samples=n_samples,
                loss_with_var=cv_loss_with_var,
                progress_bar=progress_bar,
            )
        else:
            raise ValueError('For now only marglik and gridsearch is implemented.')
        if verbose:
            print(f'Optimized prior precision is {self.prior_precision}.')

    def _gridsearch(
        self,
        loss,
        interval,
        val_loader,
        pred_type,
        link_approx='probit',
        n_samples=100,
        loss_with_var=False,
        progress_bar=False,
    ):
        assert callable(loss) or isinstance(loss, tm.Metric)

        results = list()
        prior_precs = list()
        pbar = tqdm.tqdm(interval) if progress_bar else interval
        for prior_prec in pbar:
            self.prior_precision = prior_prec
            try:
                result = validate(
                    self,
                    val_loader,
                    loss,
                    pred_type=pred_type,
                    link_approx=link_approx,
                    n_samples=n_samples,
                    loss_with_var=loss_with_var,
                )
            except RuntimeError:
                result = np.inf

            if progress_bar:
                pbar.set_description(
                    f'[Grid search | prior_prec: {prior_prec:.3e}, loss: {result:.3f}]'
                )

            results.append(result)
            prior_precs.append(prior_prec)
        return prior_precs[np.argmin(results)]

    @property
    def sigma_noise(self):
        return self._sigma_noise

    @sigma_noise.setter
    def sigma_noise(self, sigma_noise):
        self._posterior_scale = None
        if np.isscalar(sigma_noise) and np.isreal(sigma_noise):
            self._sigma_noise = torch.tensor(sigma_noise, device=self._device)
        elif torch.is_tensor(sigma_noise):
            if sigma_noise.ndim == 0:
                self._sigma_noise = sigma_noise.to(self._device)
            elif sigma_noise.ndim == 1:
                if len(sigma_noise) > 1:
                    raise ValueError('Only homoscedastic output noise supported.')
                self._sigma_noise = sigma_noise[0].to(self._device)
            else:
                raise ValueError('Sigma noise needs to be scalar or 1-dimensional.')
        else:
            raise ValueError(
                'Invalid type: sigma noise needs to be torch.Tensor or scalar.'
            )

    @property
    def _H_factor(self):
        sigma2 = self.sigma_noise.square()
        return 1 / sigma2 / self.temperature


class ParametricLaplace(BaseLaplace):
    """
    Parametric Laplace class.

    Subclasses need to specify how the Hessian approximation is initialized,
    how to add up curvature over training data, how to sample from the
    Laplace approximation, and how to compute the functional variance.

    A Laplace approximation is represented by a MAP which is given by the
    `model` parameter and a posterior precision or covariance specifying
    a Gaussian distribution \\(\\mathcal{N}(\\theta_{MAP}, P^{-1})\\).
    The goal of this class is to compute the posterior precision \\(P\\)
    which sums as
    \\[
        P = \\sum_{n=1}^N \\nabla^2_\\theta \\log p(\\mathcal{D}_n \\mid \\theta)
        \\vert_{\\theta_{MAP}} + \\nabla^2_\\theta \\log p(\\theta) \\vert_{\\theta_{MAP}}.
    \\]
    Every subclass implements different approximations to the log likelihood Hessians,
    for example, a diagonal one. The prior is assumed to be Gaussian and therefore we have
    a simple form for \\(\\nabla^2_\\theta \\log p(\\theta) \\vert_{\\theta_{MAP}} = P_0 \\).
    In particular, we assume a scalar, layer-wise, or diagonal prior precision so that in
    all cases \\(P_0 = \\textrm{diag}(p_0)\\) and the structure of \\(p_0\\) can be varied.
    """

    def __init__(
        self,
        model,
        likelihood,
        sigma_noise=1.0,
        prior_precision=1.0,
        prior_mean=0.0,
        temperature=1.0,
        enable_backprop=False,
        backend=None,
        backend_kwargs=None,
        asdl_fisher_kwargs=None,
    ):
        super().__init__(
            model,
            likelihood,
            sigma_noise,
            prior_precision,
            prior_mean,
            temperature,
            enable_backprop,
            backend,
            backend_kwargs,
            asdl_fisher_kwargs,
        )
        if not hasattr(self, 'H'):
            self._init_H()
            # posterior mean/mode
            self.mean = self.prior_mean

    def _init_H(self):
        raise NotImplementedError

    def _check_H_init(self):
        if self.H is None:
            raise AttributeError('Laplace not fitted. Run fit() first.')

    def fit(self, train_loader, override=True, progress_bar=False):
        """Fit the local Laplace approximation at the parameters of the model.

        Parameters
        ----------
        train_loader : torch.data.utils.DataLoader
            each iterate is a training batch (X, y);
            `train_loader.dataset` needs to be set to access \\(N\\), size of the data set
        override : bool, default=True
            whether to initialize H, loss, and n_data again; setting to False is useful for
            online learning settings to accumulate a sequential posterior approximation.
        progress_bar : bool, default=False
            whether to show a progress bar; updated at every batch-Hessian computation.
            Useful for very large model and large amount of data, esp. when `subset_of_weights='all'`.
        """
        if override:
            self._init_H()
            self.loss = 0
            self.n_data = 0

        self.model.eval()

        self.mean = parameters_to_vector(self.params)
        if not self.enable_backprop:
            self.mean = self.mean.detach()

        data = next(iter(train_loader))
        with torch.no_grad():
            if isinstance(data, MutableMapping):  # To support Huggingface dataset
                if isinstance(self, DiagLaplace) and self._backend_cls == CurvlinopsEF:
                    raise ValueError(
                        'Currently DiagEF is not supported under CurvlinopsEF backend '
                        + 'for custom models with non-tensor inputs '
                        + '(https://github.com/pytorch/functorch/issues/159). Consider '
                        + 'using AsdlEF backend instead.'
                    )

                out = self.model(data)
            else:
                X = data[0]
                try:
                    out = self.model(X[:1].to(self._device))
                except (TypeError, AttributeError):
                    out = self.model(X.to(self._device))
        self.n_outputs = out.shape[-1]
        setattr(self.model, 'output_size', self.n_outputs)

        N = len(train_loader.dataset)
        if progress_bar:
            pbar = tqdm.tqdm(train_loader)
            pbar.set_description('[Computing Hessian]')
        else:
            pbar = train_loader

        for data in pbar:
            if isinstance(data, MutableMapping):  # To support Huggingface dataset
                X, y = data, data['labels'].to(self._device)
            else:
                X, y = data
                X, y = X.to(self._device), y.to(self._device)
            self.model.zero_grad()
            loss_batch, H_batch = self._curv_closure(X, y, N)
            self.loss += loss_batch
            self.H += H_batch

        self.n_data += N

    @property
    def scatter(self):
        """Computes the _scatter_, a term of the log marginal likelihood that
        corresponds to L-2 regularization:
        `scatter` = \\((\\theta_{MAP} - \\mu_0)^{T} P_0 (\\theta_{MAP} - \\mu_0) \\).

        Returns
        -------
        [type]
            [description]
        """
        delta = self.mean - self.prior_mean
        return (delta * self.prior_precision_diag) @ delta

    @property
    def log_det_prior_precision(self):
        """Compute log determinant of the prior precision
        \\(\\log \\det P_0\\)

        Returns
        -------
        log_det : torch.Tensor
        """
        return self.prior_precision_diag.log().sum()

    @property
    def log_det_posterior_precision(self):
        """Compute log determinant of the posterior precision
        \\(\\log \\det P\\) which depends on the subclasses structure
        used for the Hessian approximation.

        Returns
        -------
        log_det : torch.Tensor
        """
        raise NotImplementedError

    @property
    def log_det_ratio(self):
        """Compute the log determinant ratio, a part of the log marginal likelihood.
        \\[
            \\log \\frac{\\det P}{\\det P_0} = \\log \\det P - \\log \\det P_0
        \\]

        Returns
        -------
        log_det_ratio : torch.Tensor
        """
        return self.log_det_posterior_precision - self.log_det_prior_precision

    def square_norm(self, value):
        """Compute the square norm under post. Precision with `value-self.mean` as 𝛥:
        \\[
            \\Delta^\top P \\Delta
        \\]
        Returns
        -------
        square_form
        """
        raise NotImplementedError

    def log_prob(self, value, normalized=True):
        """Compute the log probability under the (current) Laplace approximation.

        Parameters
        ----------
        normalized : bool, default=True
            whether to return log of a properly normalized Gaussian or just the
            terms that depend on `value`.

        Returns
        -------
        log_prob : torch.Tensor
        """
        if not normalized:
            return -self.square_norm(value) / 2
        log_prob = (
            -self.n_params / 2 * log(2 * pi) + self.log_det_posterior_precision / 2
        )
        log_prob -= self.square_norm(value) / 2
        return log_prob

    def log_marginal_likelihood(self, prior_precision=None, sigma_noise=None):
        """Compute the Laplace approximation to the log marginal likelihood subject
        to specific Hessian approximations that subclasses implement.
        Requires that the Laplace approximation has been fit before.
        The resulting torch.Tensor is differentiable in `prior_precision` and
        `sigma_noise` if these have gradients enabled.
        By passing `prior_precision` or `sigma_noise`, the current value is
        overwritten. This is useful for iterating on the log marginal likelihood.

        Parameters
        ----------
        prior_precision : torch.Tensor, optional
            prior precision if should be changed from current `prior_precision` value
        sigma_noise : [type], optional
            observation noise standard deviation if should be changed

        Returns
        -------
        log_marglik : torch.Tensor
        """
        # update prior precision (useful when iterating on marglik)
        if prior_precision is not None:
            self.prior_precision = prior_precision

        # update sigma_noise (useful when iterating on marglik)
        if sigma_noise is not None:
            if self.likelihood != 'regression':
                raise ValueError('Can only change sigma_noise for regression.')
            self.sigma_noise = sigma_noise

        return self.log_likelihood - 0.5 * (self.log_det_ratio + self.scatter)

    def __call__(
        self,
        x,
        pred_type='glm',
        joint=False,
        link_approx='probit',
        n_samples=100,
        diagonal_output=False,
        generator=None,
        **model_kwargs,
    ):
        """Compute the posterior predictive on input data `x`.

        Parameters
        ----------
        x : torch.Tensor or MutableMapping
            `(batch_size, input_shape)` if tensor. If MutableMapping, must contain
            the said tensor.

        pred_type : {'glm', 'nn'}, default='glm'
            type of posterior predictive, linearized GLM predictive or neural
            network sampling predictive. The GLM predictive is consistent with
            the curvature approximations used here. When Laplace is done only
            on subset of parameters (i.e. some grad are disabled),
            only `nn` predictive is supported.

        link_approx : {'mc', 'probit', 'bridge', 'bridge_norm'}
            how to approximate the classification link function for the `'glm'`.
            For `pred_type='nn'`, only 'mc' is possible.

        joint : bool
            Whether to output a joint predictive distribution in regression with
            `pred_type='glm'`. If set to `True`, the predictive distribution
            has the same form as GP posterior, i.e. N([f(x1), ...,f(xm)], Cov[f(x1), ..., f(xm)]).
            If `False`, then only outputs the marginal predictive distribution.
            Only available for regression and GLM predictive.

        n_samples : int
            number of samples for `link_approx='mc'`.

        diagonal_output : bool
            whether to use a diagonalized posterior predictive on the outputs.
            Only works for `pred_type='glm'` and `link_approx='mc'`.

        generator : torch.Generator, optional
            random number generator to control the samples (if sampling used).

        Returns
        -------
        predictive: torch.Tensor or Tuple[torch.Tensor]
            For `likelihood='classification'`, a torch.Tensor is returned with
            a distribution over classes (similar to a Softmax).
            For `likelihood='regression'`, a tuple of torch.Tensor is returned
            with the mean and the predictive variance.
            For `likelihood='regression'` and `joint=True`, a tuple of torch.Tensor
            is returned with the mean and the predictive covariance.
        """
        if pred_type not in ['glm', 'nn']:
            raise ValueError('Only glm and nn supported as prediction types.')

        if link_approx not in ['mc', 'probit', 'bridge', 'bridge_norm']:
            raise ValueError(f'Unsupported link approximation {link_approx}.')

        if pred_type == 'nn' and link_approx != 'mc':
            raise ValueError(
                'Only mc link approximation is supported for nn prediction type.'
            )

        if generator is not None:
            if (
                not isinstance(generator, torch.Generator)
                or generator.device != x.device
            ):
                raise ValueError('Invalid random generator (check type and device).')

        # For reward modeling, replace the likelihood to regression and override model state
        if self.reward_modeling and self.likelihood == 'classification':
            self.likelihood = 'regression'
            self.model.output_size = 1

        if pred_type == 'glm':
            f_mu, f_var = self._glm_predictive_distribution(
                x, joint=joint and self.likelihood == 'regression'
            )
            # regression
            if self.likelihood == 'regression':
                return f_mu, f_var
            # classification
            if link_approx == 'mc':
                return self.predictive_samples(
                    x,
                    pred_type='glm',
                    n_samples=n_samples,
                    diagonal_output=diagonal_output,
                ).mean(dim=0)
            elif link_approx == 'probit':
                kappa = 1 / torch.sqrt(1.0 + np.pi / 8 * f_var.diagonal(dim1=1, dim2=2))
                return torch.softmax(kappa * f_mu, dim=-1)
            elif 'bridge' in link_approx:
                # zero mean correction
                f_mu -= (
                    f_var.sum(-1)
                    * f_mu.sum(-1).reshape(-1, 1)
                    / f_var.sum(dim=(1, 2)).reshape(-1, 1)
                )
                f_var -= torch.einsum(
                    'bi,bj->bij', f_var.sum(-1), f_var.sum(-2)
                ) / f_var.sum(dim=(1, 2)).reshape(-1, 1, 1)
                # Laplace Bridge
                _, K = f_mu.size(0), f_mu.size(-1)
                f_var_diag = torch.diagonal(f_var, dim1=1, dim2=2)
                # optional: variance correction
                if link_approx == 'bridge_norm':
                    f_var_diag_mean = f_var_diag.mean(dim=1)
                    f_var_diag_mean /= torch.as_tensor(
                        [K / 2], device=self._device
                    ).sqrt()
                    f_mu /= f_var_diag_mean.sqrt().unsqueeze(-1)
                    f_var_diag /= f_var_diag_mean.unsqueeze(-1)
                sum_exp = torch.exp(-f_mu).sum(dim=1).unsqueeze(-1)
                alpha = (1 - 2 / K + f_mu.exp() / K**2 * sum_exp) / f_var_diag
                return torch.nan_to_num(alpha / alpha.sum(dim=1).unsqueeze(-1), nan=1.0)
        else:
            if self.likelihood == 'regression':
                samples = self._nn_predictive_samples(x, n_samples, **model_kwargs)
                return samples.mean(dim=0), samples.var(dim=0)
            else:  # classification; the average is computed online
                return self._nn_predictive_classification(x, n_samples, **model_kwargs)

    def predictive_samples(
        self, x, pred_type='glm', n_samples=100, diagonal_output=False, generator=None
    ):
        """Sample from the posterior predictive on input data `x`.
        Can be used, for example, for Thompson sampling.

        Parameters
        ----------
        x : torch.Tensor
            input data `(batch_size, input_shape)`

        pred_type : {'glm', 'nn'}, default='glm'
            type of posterior predictive, linearized GLM predictive or neural
            network sampling predictive. The GLM predictive is consistent with
            the curvature approximations used here.

        n_samples : int
            number of samples

        diagonal_output : bool
            whether to use a diagonalized glm posterior predictive on the outputs.
            Only applies when `pred_type='glm'`.

        generator : torch.Generator, optional
            random number generator to control the samples (if sampling used)

        Returns
        -------
        samples : torch.Tensor
            samples `(n_samples, batch_size, output_shape)`
        """
        if pred_type not in ['glm', 'nn']:
            raise ValueError('Only glm and nn supported as prediction types.')

        if pred_type == 'glm':
            f_mu, f_var = self._glm_predictive_distribution(x)
            assert f_var.shape == torch.Size(
                [f_mu.shape[0], f_mu.shape[1], f_mu.shape[1]]
            )
            if diagonal_output:
                f_var = torch.diagonal(f_var, dim1=1, dim2=2)
            f_samples = normal_samples(f_mu, f_var, n_samples, generator)
            if self.likelihood == 'regression':
                return f_samples
            else:
                return torch.softmax(f_samples, dim=-1)

        else:  # 'nn'
            return self._nn_predictive_samples(x, n_samples, generator)

    @torch.enable_grad()
    def _glm_predictive_distribution(self, X, joint=False):
        if 'backpack' in self._backend_cls.__name__.lower():
            # BackPACK supports backprop through Jacobians, but it interferes with functorch
            Js, f_mu = self.backend.jacobians(X, enable_backprop=self.enable_backprop)
        else:
            # For ASDL and Curvlinops, we use functorch
            Js, f_mu = self.backend.functorch_jacobians(
                X, enable_backprop=self.enable_backprop
            )

        if joint:
            f_mu = f_mu.flatten()  # (batch*out)
            f_var = self.functional_covariance(Js)  # (batch*out, batch*out)
        else:
            f_var = self.functional_variance(Js)

        return (
            (f_mu.detach(), f_var.detach())
            if not self.enable_backprop
            else (f_mu, f_var)
        )

    def _nn_predictive_samples(self, X, n_samples=100, generator=None, **model_kwargs):
        fs = list()
        for sample in self.sample(n_samples, generator):
            vector_to_parameters(sample, self.params)
            logits = self.model(
                X.to(self._device) if isinstance(X, torch.Tensor) else X, **model_kwargs
            )
            fs.append(logits.detach() if not self.enable_backprop else logits)
        vector_to_parameters(self.mean, self.params)
        fs = torch.stack(fs)
        if self.likelihood == 'classification':
            fs = torch.softmax(fs, dim=-1)
        return fs

    def _nn_predictive_classification(self, X, n_samples=100, **model_kwargs):
        py = 0
        for sample in self.sample(n_samples):
            vector_to_parameters(sample, self.params)
            logits = self.model(
                X.to(self._device) if isinstance(X, torch.Tensor) else X, **model_kwargs
            ).detach()
            py += torch.softmax(logits, dim=-1) / n_samples
        vector_to_parameters(self.mean, self.params)
        return py

    def functional_variance(self, Jacs):
        """Compute functional variance for the `'glm'` predictive:
        `f_var[i] = Jacs[i] @ P.inv() @ Jacs[i].T`, which is a output x output
        predictive covariance matrix.
        Mathematically, we have for a single Jacobian
        \\(\\mathcal{J} = \\nabla_\\theta f(x;\\theta)\\vert_{\\theta_{MAP}}\\)
        the output covariance matrix
        \\( \\mathcal{J} P^{-1} \\mathcal{J}^T \\).

        Parameters
        ----------
        Jacs : torch.Tensor
            Jacobians of model output wrt parameters
            `(batch, outputs, parameters)`

        Returns
        -------
        f_var : torch.Tensor
            output covariance `(batch, outputs, outputs)`
        """
        raise NotImplementedError

    def functional_covariance(self, Jacs):
        """Compute functional covariance for the `'glm'` predictive:
        `f_cov = Jacs @ P.inv() @ Jacs.T`, which is a batch*output x batch*output
        predictive covariance matrix.

        This emulates the GP posterior covariance N([f(x1), ...,f(xm)], Cov[f(x1), ..., f(xm)]).
        Useful for joint predictions, such as in batched Bayesian optimization.

        Parameters
        ----------
        Jacs : torch.Tensor
            Jacobians of model output wrt parameters
            `(batch*outputs, parameters)`

        Returns
        -------
        f_cov : torch.Tensor
            output covariance `(batch*outputs, batch*outputs)`
        """
        raise NotImplementedError

    def sample(self, n_samples=100, generator=None):
        """Sample from the Laplace posterior approximation, i.e.,
        \\( \\theta \\sim \\mathcal{N}(\\theta_{MAP}, P^{-1})\\).

        Parameters
        ----------
        n_samples : int, default=100
            number of samples

        generator : torch.Generator, optional
            random number generator to control the samples
        """
        raise NotImplementedError

    def optimize_prior_precision(
        self,
        method='marglik',
        pred_type='glm',
        n_steps=100,
        lr=1e-1,
        init_prior_prec=1.0,
        prior_structure='scalar',
        val_loader=None,
        loss=None,
        log_prior_prec_min=-4,
        log_prior_prec_max=4,
        grid_size=100,
        link_approx='probit',
        n_samples=100,
        verbose=False,
        cv_loss_with_var=False,
        progress_bar=False,
    ):
        assert pred_type in ['glm', 'nn']
        self.optimize_prior_precision_base(
            pred_type,
            method,
            n_steps,
            lr,
            init_prior_prec,
            prior_structure,
            val_loader,
            loss,
            log_prior_prec_min,
            log_prior_prec_max,
            grid_size,
            link_approx,
            n_samples,
            verbose,
            cv_loss_with_var,
            progress_bar,
        )

    @property
    def posterior_precision(self):
        """Compute or return the posterior precision \\(P\\).

        Returns
        -------
        posterior_prec : torch.Tensor
        """
        raise NotImplementedError

    def state_dict(self) -> dict:
        self._check_H_init()
        state_dict = {
            'mean': self.mean,
            'H': self.H,
            'loss': self.loss,
            'prior_mean': self.prior_mean,
            'prior_precision': self.prior_precision,
            'sigma_noise': self.sigma_noise,
            'n_data': self.n_data,
            'n_outputs': self.n_outputs,
            'likelihood': self.likelihood,
            'temperature': self.temperature,
            'enable_backprop': self.enable_backprop,
            'cls_name': self.__class__.__name__,
        }
        return state_dict

    def load_state_dict(self, state_dict: dict):
        # Dealbreaker errors
        if self.__class__.__name__ != state_dict['cls_name']:
            raise ValueError(
                'Loading a wrong Laplace type. Make sure `subset_of_weights` and'
                + ' `hessian_structure` are correct!'
            )
        if self.n_params is not None and len(state_dict['mean']) != self.n_params:
            raise ValueError(
                'Attempting to load Laplace with different number of parameters than the model.'
                + ' Make sure that you use the same `subset_of_weights` value and the same `.requires_grad`'
                + ' switch on `model.parameters()`.'
            )
        if self.likelihood != state_dict['likelihood']:
            raise ValueError('Different likelihoods detected!')

        # Ignorable warnings
        if self.prior_mean is None and state_dict['prior_mean'] is not None:
            warnings.warn(
                'Loading non-`None` prior mean into a `None` prior mean. You might get wrong results.'
            )
        if self.temperature != state_dict['temperature']:
            warnings.warn(
                'Different `temperature` parameters detected. Some calculation might be off!'
            )
        if self.enable_backprop != state_dict['enable_backprop']:
            warnings.warn(
                'Different `enable_backprop` values. You might encounter error when differentiating'
                + ' the predictive mean and variance.'
            )

        self.mean = state_dict['mean']
        self.H = state_dict['H']
        self.loss = state_dict['loss']
        self.prior_mean = state_dict['prior_mean']
        self.prior_precision = state_dict['prior_precision']
        self.sigma_noise = state_dict['sigma_noise']
        self.n_data = state_dict['n_data']
        self.n_outputs = state_dict['n_outputs']
        setattr(self.model, 'output_size', self.n_outputs)
        self.likelihood = state_dict['likelihood']
        self.temperature = state_dict['temperature']
        self.enable_backprop = state_dict['enable_backprop']


class FullLaplace(ParametricLaplace):
    """Laplace approximation with full, i.e., dense, log likelihood Hessian approximation
    and hence posterior precision. Based on the chosen `backend` parameter, the full
    approximation can be, for example, a generalized Gauss-Newton matrix.
    Mathematically, we have \\(P \\in \\mathbb{R}^{P \\times P}\\).
    See `BaseLaplace` for the full interface.
    """

    # key to map to correct subclass of BaseLaplace, (subset of weights, Hessian structure)
    _key = ('all', 'full')

    def __init__(
        self,
        model,
        likelihood,
        sigma_noise=1.0,
        prior_precision=1.0,
        prior_mean=0.0,
        temperature=1.0,
        enable_backprop=False,
        backend=None,
        backend_kwargs=None,
    ):
        super().__init__(
            model,
            likelihood,
            sigma_noise,
            prior_precision,
            prior_mean,
            temperature,
            enable_backprop,
            backend,
            backend_kwargs,
        )
        self._posterior_scale = None

    def _init_H(self):
        self.H = torch.zeros(self.n_params, self.n_params, device=self._device)

    def _curv_closure(self, X, y, N):
        return self.backend.full(X, y, N=N)

    def fit(self, train_loader, override=True, progress_bar=False):
        self._posterior_scale = None
        return super().fit(train_loader, override=override, progress_bar=progress_bar)

    def _compute_scale(self):
        self._posterior_scale = invsqrt_precision(self.posterior_precision)

    @property
    def posterior_scale(self):
        """Posterior scale (square root of the covariance), i.e.,
        \\(P^{-\\frac{1}{2}}\\).

        Returns
        -------
        scale : torch.tensor
            `(parameters, parameters)`
        """
        if self._posterior_scale is None:
            self._compute_scale()
        return self._posterior_scale

    @property
    def posterior_covariance(self):
        """Posterior covariance, i.e., \\(P^{-1}\\).

        Returns
        -------
        covariance : torch.tensor
            `(parameters, parameters)`
        """
        scale = self.posterior_scale
        return scale @ scale.T

    @property
    def posterior_precision(self):
        """Posterior precision \\(P\\).

        Returns
        -------
        precision : torch.tensor
            `(parameters, parameters)`
        """
        self._check_H_init()
        return self._H_factor * self.H + torch.diag(self.prior_precision_diag)

    @property
    def log_det_posterior_precision(self):
        return self.posterior_precision.logdet()

    def square_norm(self, value):
        delta = value - self.mean
        return delta @ self.posterior_precision @ delta

    def functional_variance(self, Js):
        return torch.einsum('ncp,pq,nkq->nck', Js, self.posterior_covariance, Js)

    def functional_covariance(self, Js):
        n_batch, n_outs, n_params = Js.shape
        Js = Js.reshape(n_batch * n_outs, n_params)
        return torch.einsum('np,pq,mq->nm', Js, self.posterior_covariance, Js)

    def sample(self, n_samples=100, generator=None):
        samples = torch.randn(
            n_samples, self.n_params, device=self._device, generator=generator
        )
        # (n_samples, n_params) x (n_params, n_params) -> (n_samples, n_params)
        samples = samples @ self.posterior_scale
        return self.mean.reshape(1, self.n_params) + samples


class KronLaplace(ParametricLaplace):
    """Laplace approximation with Kronecker factored log likelihood Hessian approximation
    and hence posterior precision.
    Mathematically, we have for each parameter group, e.g., torch.nn.Module,
    that \\P\\approx Q \\otimes H\\.
    See `BaseLaplace` for the full interface and see
    `laplace.utils.matrix.Kron` and `laplace.utils.matrix.KronDecomposed` for the structure of
    the Kronecker factors. `Kron` is used to aggregate factors by summing up and
    `KronDecomposed` is used to add the prior, a Hessian factor (e.g. temperature),
    and computing posterior covariances, marginal likelihood, etc.
    Damping can be enabled by setting `damping=True`.
    """

    # key to map to correct subclass of BaseLaplace, (subset of weights, Hessian structure)
    _key = ('all', 'kron')

    def __init__(
        self,
        model,
        likelihood,
        sigma_noise=1.0,
        prior_precision=1.0,
        prior_mean=0.0,
        temperature=1.0,
        enable_backprop=False,
        backend=None,
        damping=False,
        backend_kwargs=None,
        asdl_fisher_kwargs=None,
    ):
        self.damping = damping
        self.H_facs = None
        super().__init__(
            model,
            likelihood,
            sigma_noise,
            prior_precision,
            prior_mean,
            temperature,
            enable_backprop,
            backend,
            backend_kwargs,
            asdl_fisher_kwargs,
        )

    def _init_H(self):
        self.H = Kron.init_from_model(self.params, self._device)

    def _curv_closure(self, X, y, N):
        return self.backend.kron(X, y, N=N, **self._asdl_fisher_kwargs)

    @staticmethod
    def _rescale_factors(kron, factor):
        for F in kron.kfacs:
            if len(F) == 2:
                F[1] *= factor
        return kron

    def fit(self, train_loader, override=True, progress_bar=False):
        if override:
            self.H_facs = None

        if self.H_facs is not None:
            n_data_old = self.n_data
            n_data_new = len(train_loader.dataset)
            self._init_H()  # re-init H non-decomposed
            # discount previous Kronecker factors to sum up properly together with new ones
            self.H_facs = self._rescale_factors(
                self.H_facs, n_data_old / (n_data_old + n_data_new)
            )

        super().fit(train_loader, override=override, progress_bar=progress_bar)

        if self.H_facs is None:
            self.H_facs = self.H
        else:
            # discount new factors that were computed assuming N = n_data_new
            self.H = self._rescale_factors(
                self.H, n_data_new / (n_data_new + n_data_old)
            )
            self.H_facs += self.H
        # Decompose to self.H for all required quantities but keep H_facs for further inference
        self.H = self.H_facs.decompose(damping=self.damping)

    @property
    def posterior_precision(self):
        """Kronecker factored Posterior precision \\(P\\).

        Returns
        -------
        precision : `laplace.utils.matrix.KronDecomposed`
        """
        self._check_H_init()
        return self.H * self._H_factor + self.prior_precision

    @property
    def log_det_posterior_precision(self):
        if type(self.H) is Kron:  # Fall back to diag prior
            return self.prior_precision_diag.log().sum()
        return self.posterior_precision.logdet()

    def square_norm(self, value):
        delta = value - self.mean
        if type(self.H) is Kron:  # fall back to prior
            return (delta * self.prior_precision_diag) @ delta
        return delta @ self.posterior_precision.bmm(delta, exponent=1)

    def functional_variance(self, Js):
        return self.posterior_precision.inv_square_form(Js)

    def functional_covariance(self, Js):
        self._check_jacobians(Js)
        n_batch, n_outs, n_params = Js.shape
        Js = Js.reshape(n_batch * n_outs, n_params).unsqueeze(0)
        cov = self.posterior_precision.inv_square_form(Js).squeeze(0)
        assert cov.shape == (n_batch * n_outs, n_batch * n_outs)
        return cov

    def sample(self, n_samples=100, generator=None):
        samples = torch.randn(
            n_samples, self.n_params, device=self._device, generator=generator
        )
        samples = self.posterior_precision.bmm(samples, exponent=-0.5)
        return self.mean.reshape(1, self.n_params) + samples.reshape(
            n_samples, self.n_params
        )

    @BaseLaplace.prior_precision.setter
    def prior_precision(self, prior_precision):
        # Extend setter from Laplace to restrict prior precision structure.
        super(KronLaplace, type(self)).prior_precision.fset(self, prior_precision)
        if len(self.prior_precision) not in [1, self.n_layers]:
            raise ValueError('Prior precision for Kron either scalar or per-layer.')

    def state_dict(self) -> dict:
        state_dict = super().state_dict()
        state_dict['H'] = self.H_facs.kfacs
        return state_dict

    def load_state_dict(self, state_dict: dict):
        super().load_state_dict(state_dict)
        self._init_H()
        self.H_facs = self.H
        self.H_facs.kfacs = state_dict['H']
        self.H = self.H_facs.decompose(damping=self.damping)


class LowRankLaplace(ParametricLaplace):
    """Laplace approximation with low-rank log likelihood Hessian (approximation).
    The low-rank matrix is represented by an eigendecomposition (vecs, values).
    Based on the chosen `backend`, either a true Hessian or, for example, GGN
    approximation could be used.
    The posterior precision is computed as
    \\( P = V diag(l) V^T + P_0.\\)
    To sample, compute the functional variance, and log determinant, algebraic tricks
    are usedto reduce the costs of inversion to the that of a \\(K \times K\\) matrix
    if we have a rank of K.

    See `BaseLaplace` for the full interface.
    """

    _key = ('all', 'lowrank')

    def __init__(
        self,
        model,
        likelihood,
        sigma_noise=1,
        prior_precision=1,
        prior_mean=0,
        temperature=1,
        enable_backprop=False,
        backend=AsdfghjklHessian,
        backend_kwargs=None,
    ):
        super().__init__(
            model,
            likelihood,
            sigma_noise=sigma_noise,
            prior_precision=prior_precision,
            prior_mean=prior_mean,
            temperature=temperature,
            enable_backprop=enable_backprop,
            backend=backend,
            backend_kwargs=backend_kwargs,
        )

    def _init_H(self):
        self.H = None

    @property
    def V(self):
        (U, _), prior_prec_diag = self.posterior_precision
        return U / prior_prec_diag.reshape(-1, 1)

    @property
    def Kinv(self):
        (U, eig), _ = self.posterior_precision
        return torch.inverse(torch.diag(1 / eig) + U.T @ self.V)

    def fit(self, train_loader, override=True):
        # override fit since output of eighessian not additive across batch
        if not override:
            # LowRankLA cannot be updated since eigenvalue representation not additive
            raise ValueError('LowRank LA does not support updating.')

        self.model.eval()
        self.mean = parameters_to_vector(self.model.parameters())

        if not self.enable_backprop:
            self.mean = self.mean.detach()

        X, _ = next(iter(train_loader))
        with torch.no_grad():
            try:
                out = self.model(X[:1].to(self._device))
            except (TypeError, AttributeError):
                out = self.model(X.to(self._device))
        self.n_outputs = out.shape[-1]
        setattr(self.model, 'output_size', self.n_outputs)

        eigenvectors, eigenvalues, loss = self.backend.eig_lowrank(train_loader)
        self.H = (eigenvectors, eigenvalues)
        self.loss = loss

        self.n_data = len(train_loader.dataset)

    @property
    def posterior_precision(self):
        """Return correctly scaled posterior precision that would be constructed
        as H[0] @ diag(H[1]) @ H[0].T + self.prior_precision_diag.

        Returns
        -------
        H : tuple(eigenvectors, eigenvalues)
            scaled self.H with temperature and loss factors.
        prior_precision_diag : torch.Tensor
            diagonal prior precision shape `parameters` to be added to H.
        """
        self._check_H_init()
        return (self.H[0], self._H_factor * self.H[1]), self.prior_precision_diag

    def functional_variance(self, Jacs):
        prior_var = torch.einsum('ncp,nkp->nck', Jacs / self.prior_precision_diag, Jacs)
        Jacs_V = torch.einsum('ncp,pl->ncl', Jacs, self.V)
        info_gain = torch.einsum('ncl,nkl->nck', Jacs_V @ self.Kinv, Jacs_V)
        return prior_var - info_gain

    def functional_covariance(self, Jacs):
        n_batch, n_outs, n_params = Jacs.shape
        Jacs = Jacs.reshape(n_batch * n_outs, n_params)
        prior_cov = torch.einsum('np,mp->nm', Jacs / self.prior_precision_diag, Jacs)
        Jacs_V = torch.einsum('np,pl->nl', Jacs, self.V)
        info_gain = torch.einsum('nl,ml->nm', Jacs_V @ self.Kinv, Jacs_V)
        cov = prior_cov - info_gain
        assert cov.shape == (n_batch * n_outs, n_batch * n_outs)
        return cov

    def sample(self, n_samples, generator=None):
        samples = torch.randn(self.n_params, n_samples, generator=generator)
        d = self.prior_precision_diag
        Vs = self.V * d.sqrt().reshape(-1, 1)
        VtV = Vs.T @ Vs
        Ik = torch.eye(len(VtV))
        A = torch.linalg.cholesky(VtV)
        B = torch.linalg.cholesky(VtV + Ik)
        A_inv = torch.inverse(A)
        C = torch.inverse(A_inv.T @ (B - Ik) @ A_inv)
        Kern_inv = torch.inverse(torch.inverse(C) + Vs.T @ Vs)
        dinv_sqrt = (d).sqrt().reshape(-1, 1)
        prior_sample = dinv_sqrt * samples
        gain_sample = dinv_sqrt * Vs @ Kern_inv @ (Vs.T @ samples)
        return self.mean + (prior_sample - gain_sample).T

    @property
    def log_det_posterior_precision(self):
        (_, eig), prior_prec_diag = self.posterior_precision
        return eig.log().sum() + prior_prec_diag.log().sum() - torch.logdet(self.Kinv)


class DiagLaplace(ParametricLaplace):
    """Laplace approximation with diagonal log likelihood Hessian approximation
    and hence posterior precision.
    Mathematically, we have \\(P \\approx \\textrm{diag}(P)\\).
    See `BaseLaplace` for the full interface.
    """

    # key to map to correct subclass of BaseLaplace, (subset of weights, Hessian structure)
    _key = ('all', 'diag')

    def _init_H(self):
        self.H = torch.zeros(self.n_params, device=self._device)

    def _curv_closure(self, X, y, N):
        return self.backend.diag(X, y, N=N, **self._asdl_fisher_kwargs)

    @property
    def posterior_precision(self):
        """Diagonal posterior precision \\(p\\).

        Returns
        -------
        precision : torch.tensor
            `(parameters)`
        """
        self._check_H_init()
        return self._H_factor * self.H + self.prior_precision_diag

    @property
    def posterior_scale(self):
        """Diagonal posterior scale \\(\\sqrt{p^{-1}}\\).

        Returns
        -------
        precision : torch.tensor
            `(parameters)`
        """
        return 1 / self.posterior_precision.sqrt()

    @property
    def posterior_variance(self):
        """Diagonal posterior variance \\(p^{-1}\\).

        Returns
        -------
        precision : torch.tensor
            `(parameters)`
        """
        return 1 / self.posterior_precision

    @property
    def log_det_posterior_precision(self):
        return self.posterior_precision.log().sum()

    def square_norm(self, value):
        delta = value - self.mean
        return delta @ (delta * self.posterior_precision)

    def functional_variance(self, Js: torch.Tensor) -> torch.Tensor:
        self._check_jacobians(Js)
        return torch.einsum('ncp,p,nkp->nck', Js, self.posterior_variance, Js)

    def functional_covariance(self, Js):
        self._check_jacobians(Js)
        n_batch, n_outs, n_params = Js.shape
        Js = Js.reshape(n_batch * n_outs, n_params)
        cov = torch.einsum('np,p,mp->nm', Js, self.posterior_variance, Js)
        return cov

    def sample(self, n_samples=100, generator=None):
        samples = torch.randn(
            n_samples, self.n_params, device=self._device, generator=generator
        )
        samples = samples * self.posterior_scale.reshape(1, self.n_params)
        return self.mean.reshape(1, self.n_params) + samples


class FunctionalLaplace(BaseLaplace):
    """
    Applying the GGN (General Gauss Newton) approximation for the Hessian in the Laplace approximation of the posterior
    turns the underlying probabilistic model from a BNN into a GLM (generalized linear model).
    This GLM (in the weight space) is equivalent to a GP (in the function space), see
    [Approximate Inference Turns Deep Networks into Gaussian Processes (Khan et al., 2019)](https://arxiv.org/abs/1906.01930)

    This class implements the (approximate) GP inference through which
    we obtain the desired quantities (posterior predictive, marginal log-likelihood).
    See [Improving predictions of Bayesian neural nets via local linearization (Immer et al., 2021)](https://arxiv.org/abs/2008.08400)
    for more details.

    Note that for `likelihood='classification'`, we approximate \( L_{NN} \\) with a diagonal matrix
    ( \\( L_{NN} \\) is a block-diagonal matrix, where blocks represent Hessians of per-data-point log-likelihood w.r.t.
    neural network output \\( f \\), See Appendix [A.2.1](https://arxiv.org/abs/2008.08400) for exact definition). We
    resort to such an approximation because of the (possible) errors found in Laplace approximation for
    multiclass GP classification in Chapter 3.5 of [R&W 2006 GP book](http://www.gaussianprocess.org/gpml/),
    see the question
    [here](https://stats.stackexchange.com/questions/555183/gaussian-processes-multi-class-laplace-approximation)
    for more details. Alternatively, one could also resort to *one-vs-one* or *one-vs-rest* implementations
    for multiclass classification, however, that is not (yet) supported here.

    Parameters
    ----------
    M : int
        number of data points for Subset-of-Data (SOD) approximate GP inference.
        By default (`M=None`), all data points from train dataset are used
    diagonal_kernel : bool
        GP kernel here is product of Jacobians, which results in a \\( C \\times C\\) matrix where \\(C\\) is the output
        dimension. If `diagonal_kernel=True`, only a diagonal of a GP kernel is used. This is (somewhat) equivalent to
        assuming independent GPs across output channels.

    See `BaseLaplace` class for the full interface.
    """

    # key to map to correct subclass of BaseLaplace, (subset of weights, Hessian structure)
    _key = ('all', 'gp')

    def __init__(
        self,
        model,
        likelihood,
        M=None,
        sigma_noise=1.0,
        prior_precision=1.0,
        prior_mean=0.0,
        temperature=1.0,
        backend=BackPackGGN,
        backend_kwargs=None,
        enable_backprop=False,
        diagonal_kernel=False,
        seed=0,
    ):
        assert backend in [BackPackGGN, AsdlGGN]
        self._check_prior_precision(prior_precision)
        super().__init__(
            model,
            likelihood,
            sigma_noise,
            prior_precision,
            prior_mean,
            temperature,
            backend,
            backend_kwargs,
        )
        self.enable_backprop = enable_backprop

        self.M = M
        self.diagonal_kernel = diagonal_kernel
        self.seed = seed

        self.K_MM = None
        self.Sigma_inv = None  # (K_{MM} + L_MM_inv)^{-1}
        self.train_loader = (
            None  # needed in functional variance and marginal log likelihood
        )
        self.batch_size = None
        self._prior_factor_sod = None
        self.mu = None  # mean in the scatter term of the log marginal likelihood
        self.L = None

        # MAP estimate of NN parameters (used in regression marginal likelihood)
        self.map_estimate = parameters_to_vector(self.model.parameters()).detach()

        self._fitted = False
        self._recompute_Sigma = True

    @staticmethod
    def _check_prior_precision(prior_precision):
        """
        Checks if the given prior precision is suitable for the GP interpretation of LLA.
        As such, only single value priors, i.e., isotropic priors are suitable.
        """
        if torch.is_tensor(prior_precision):
            if not (
                prior_precision.ndim == 0
                or (prior_precision.ndim == 1 and len(prior_precision) == 1)
            ):
                raise ValueError('Only isotropic priors supported in FunctionalLaplace')

    def _init_K_MM(self):
        """
        Allocates memory for the kernel matrix evaluated at the subset of the training
        data points. If the subset is of size \(M\) and the problem has \(C\) outputs,
        this is a list of C \((M,M\)) tensors for diagonal kernel and \((M x C, M x C)\)
        otherwise.
        """
        if self.diagonal_kernel:
            self.K_MM = [
                torch.empty(size=(self.M, self.M), device=self._device)
                for _ in range(self.n_outputs)
            ]
        else:
            self.K_MM = torch.empty(
                size=(self.M * self.n_outputs, self.M * self.n_outputs),
                device=self._device,
            )

    def _init_Sigma_inv(self):
        """
        Allocates memory for the cholesky decomposition of
        \[
            K_{MM} + \Lambda_{MM}^{-1}.
        \]
        See See [Improving predictions of Bayesian neural nets via local linearization (Immer et al., 2021)](https://arxiv.org/abs/2008.08400)
        Equation 15 for more information.
        """
        if self.diagonal_kernel:
            self.Sigma_inv = [
                torch.empty(size=(self.M, self.M), device=self._device)
                for _ in range(self.n_outputs)
            ]
        else:
            self.Sigma_inv = torch.empty(
                size=(self.M * self.n_outputs, self.M * self.n_outputs),
                device=self._device,
            )

    def _store_K_batch(self, K_batch, i, j):
        """
        Given the kernel matrix between the i-th and the j-th batch, stores it in the
        corresponding position in self.K_MM.
        """
        if self.diagonal_kernel:
            for c in range(self.n_outputs):
                self.K_MM[c][
                    i * self.batch_size : min((i + 1) * self.batch_size, self.M),
                    j * self.batch_size : min((j + 1) * self.batch_size, self.M),
                ] = K_batch[:, :, c]
                if i != j:
                    self.K_MM[c][
                        j * self.batch_size : min((j + 1) * self.batch_size, self.M),
                        i * self.batch_size : min((i + 1) * self.batch_size, self.M),
                    ] = torch.transpose(K_batch[:, :, c], 0, 1)
        else:
            bC = self.batch_size * self.n_outputs
            MC = self.M * self.n_outputs
            self.K_MM[
                i * bC : min((i + 1) * bC, MC), j * bC : min((j + 1) * bC, MC)
            ] = K_batch
            if i != j:
                self.K_MM[
                    j * bC : min((j + 1) * bC, MC), i * bC : min((i + 1) * bC, MC)
                ] = torch.transpose(K_batch, 0, 1)

    def _build_L(self, lambdas):
        """
        Given a list of the Hessians of per-batch log-likelihood w.r.t. neural network output \\( f \\),
        returns the contatenation of these hessians in a suitable format for the used kernel
        (diagonal or not).

        In this function the diagonal approximation is performed. Please refer to the introduction of the
        class for more details.

        Parameters
        ----------
        lambdas : list of torch.Tensor of shape (C, C)
                  Contains per-batch log-likelihood w.r.t. neural network output \\( f \\).

        Returns
        -------
        L : list with length C of tensors with shape M or tensor (MxC)
            Contains the given Hessians in a suitable format.
        """
        # Concatenate batch dimension and discard non-diagonal entries.
        L_diag = torch.diagonal(torch.cat(lambdas, dim=0), dim1=-2, dim2=-1).reshape(-1)

        if self.diagonal_kernel:
            return [L_diag[i :: self.n_outputs] for i in range(self.n_outputs)]
        else:
            return L_diag

    def _build_Sigma_inv(self):
        """
        Computes the cholesky decomposition of
        \[
            K_{MM} + \Lambda_{MM}^{-1}.
        \]
        See See [Improving predictions of Bayesian neural nets via local linearization (Immer et al., 2021)](https://arxiv.org/abs/2008.08400)
        Equation 15 for more information.

        As the diagonal approximation is performed with \Lambda_{MM} (which is stored in self.L),
        the code is greatly simplified.
        """
        if self.diagonal_kernel:
            self.Sigma_inv = [
                torch.linalg.cholesky(
                    self.gp_kernel_prior_variance * self.K_MM[c]
                    + torch.diag(
                        torch.nan_to_num(1.0 / (self._H_factor * lambda_c), posinf=10.0)
                    )
                )
                for c, lambda_c in enumerate(self.L)
            ]
        else:
            self.Sigma_inv = torch.linalg.cholesky(
                self.gp_kernel_prior_variance * self.K_MM
                + torch.diag(
                    torch.nan_to_num(1 / (self._H_factor * self.L), posinf=10.0)
                )
            )

    def _get_SoD_data_loader(self, train_loader: DataLoader) -> DataLoader:
        """
        Subset-of-Datapoints data loader
        """
        return DataLoader(
            dataset=train_loader.dataset,
            batch_size=train_loader.batch_size,
            sampler=SoDSampler(N=len(train_loader.dataset), M=self.M, seed=self.seed),
            shuffle=False,
        )

    def fit(self, train_loader, progress_bar=False):
        """Fit the Laplace approximation of a GP posterior.

        Parameters
        ----------
        train_loader : torch.data.utils.DataLoader
            `train_loader.dataset` needs to be set to access \\(N\\), size of the data set
            `train_loader.batch_size` needs to be set to access \\(b\\) batch_size
        """

        X, _ = next(iter(train_loader))
        with torch.no_grad():
            self.n_outputs = self.model(X[:1].to(self._device)).shape[-1]
        setattr(self.model, 'output_size', self.n_outputs)
        self.batch_size = train_loader.batch_size

        if (
            self.likelihood == 'regression'
            and self.n_outputs > 1
            and self.diagonal_kernel
        ):
            warnings.warn(
                'Using FunctionalLaplace with the diagonal approximation of a GP kernel is not recommended '
                'in the case of multivariate regression. Predictive variance will likely be overestimated.'
            )

        self.model.eval()

        N = len(train_loader.dataset)
        self.n_data = N

        # by default, all training data points are used for GP inference
        if self.M is None:
            self.M = N

        train_loader = self._get_SoD_data_loader(train_loader)
        self.train_loader = train_loader
        self._prior_factor_sod = self.M / self.n_data

        self._init_K_MM()
        self._init_Sigma_inv()

        f, lambdas, mu = [], [], []

        if progress_bar:
            loader = enumerate(tqdm.tqdm(train_loader, desc='Fitting'))
        else:
            loader = enumerate(train_loader)

        for i, (X, y) in loader:
            X, y = X.to(self._device), y.to(self._device)

            Js_batch, f_batch = self._jacobians(X)
            loss_batch = self.backend.factor * self.backend.lossfunc(f_batch, y)

            if self.likelihood == 'regression':
                b, C = f_batch.shape
                lambdas_batch = torch.unsqueeze(torch.eye(C), 0).repeat(b, 1, 1)
            else:
                # second derivative of log lik is diag(p) - pp^T
                ps = torch.softmax(f_batch, dim=-1)
                lambdas_batch = torch.diag_embed(ps) - torch.einsum(
                    'mk,mc->mck', ps, ps
                )

            self.loss += loss_batch
            lambdas.append(lambdas_batch)
            f.append(f_batch)
            mu.append(
                self._mean_scatter_term_batch(Js_batch, f_batch, y)
            )  # needed for marginal likelihood
            for j, (X2, _) in enumerate(train_loader):
                if j >= i:
                    X2 = X2.to(self._device)
                    K_batch = self._kernel_batch(Js_batch, X2)
                    self._store_K_batch(K_batch, i, j)

        self.L = self._build_L(lambdas)
        self.mu = torch.cat(mu, dim=0)
        self._build_Sigma_inv()
        self._fitted = True

    @torch.enable_grad()
    def _glm_predictive_distribution(self, X, joint=False):
        Js, f_mu = self._jacobians(X)

        if joint:
            f_mu = f_mu.flatten()  # (batch*out)
            f_var = self.functional_covariance(Js)  # (batch*out, batch*out)
        else:
            f_var = self.functional_variance(Js)

        return (
            (f_mu.detach(), f_var.detach())
            if not self.enable_backprop
            else (f_mu, f_var)
        )

    def __call__(
        self,
        x,
        pred_type='gp',
        joint=False,
        link_approx='probit',
        n_samples=100,
        diagonal_output=False,
        generator=None,
        **model_kwargs,
    ):
        """Compute the posterior predictive on input data `x`.

        Parameters
        ----------
        x : torch.Tensor or MutableMapping
            `(batch_size, input_shape)` if tensor. If MutableMapping, must contain
            the said tensor.

        pred_type : {'gp'}, default='gp'
            type of posterior predictive, linearized GLM predictive (GP).
            The GP predictive is consistent with
            the curvature approximations used here.

        link_approx : {'mc', 'probit', 'bridge', 'bridge_norm'}
            how to approximate the classification link function for the `'glm'`.

        joint : bool
            Whether to output a joint predictive distribution in regression with
            `pred_type='glm'`. If set to `True`, the predictive distribution
            has the same form as GP posterior, i.e. N([f(x1), ...,f(xm)], Cov[f(x1), ..., f(xm)]).
            If `False`, then only outputs the marginal predictive distribution.
            Only available for regression and GLM predictive.

        n_samples : int
            number of samples for `link_approx='mc'`.

        diagonal_output : bool
            whether to use a diagonalized posterior predictive on the outputs.
            Only works for `pred_type='glm'` and `link_approx='mc'`.

        generator : torch.Generator, optional
            random number generator to control the samples (if sampling used).

        Returns
        -------
        predictive: torch.Tensor or Tuple[torch.Tensor]
            For `likelihood='classification'`, a torch.Tensor is returned with
            a distribution over classes (similar to a Softmax).
            For `likelihood='regression'`, a tuple of torch.Tensor is returned
            with the mean and the predictive variance.
            For `likelihood='regression'` and `joint=True`, a tuple of torch.Tensor
            is returned with the mean and the predictive covariance.
        """

        if self._fitted is False:
            raise RuntimeError(
                'Functional Laplace has not been fitted to any '
                + 'training dataset. Please call .fit method.'
            )

        if self._recompute_Sigma is True:
            Warning(
                'The prior precision has been changed since fit. '
                + 'Re-compututing its value...'
            )
            self._build_Sigma_inv()

        if pred_type not in ['gp']:
            raise ValueError('Only gp supported as prediction types.')

        if link_approx not in ['mc', 'probit', 'bridge', 'bridge_norm']:
            raise ValueError(f'Unsupported link approximation {link_approx}.')

        if generator is not None:
            if (
                not isinstance(generator, torch.Generator)
                or generator.device != x.device
            ):
                raise ValueError('Invalid random generator (check type and device).')

        # For reward modeling, replace the likelihood to regression and override model state
        if self.reward_modeling and self.likelihood == 'classification':
            self.likelihood = 'regression'
            self.model.output_size = 1

        f_mu, f_var = self._glm_predictive_distribution(
            x, joint=joint and self.likelihood == 'regression'
        )
        # regression
        if self.likelihood == 'regression':
            return f_mu, f_var
        # classification
        if link_approx == 'mc':
            return self.predictive_samples(
                f_mu,
                f_var,
                n_samples=n_samples,
            ).mean(dim=0)
        elif link_approx == 'probit':
            kappa = 1 / torch.sqrt(1.0 + np.pi / 8 * f_var.diagonal(dim1=1, dim2=2))
            return torch.softmax(kappa * f_mu, dim=-1)
        elif 'bridge' in link_approx:
            # zero mean correction
            f_mu -= (
                f_var.sum(-1)
                * f_mu.sum(-1).reshape(-1, 1)
                / f_var.sum(dim=(1, 2)).reshape(-1, 1)
            )
            f_var -= torch.einsum(
                'bi,bj->bij', f_var.sum(-1), f_var.sum(-2)
            ) / f_var.sum(dim=(1, 2)).reshape(-1, 1, 1)
            # Laplace Bridge
            _, K = f_mu.size(0), f_mu.size(-1)
            f_var_diag = torch.diagonal(f_var, dim1=1, dim2=2)
            # optional: variance correction
            if link_approx == 'bridge_norm':
                f_var_diag_mean = f_var_diag.mean(dim=1)
                f_var_diag_mean /= torch.as_tensor([K / 2], device=self._device).sqrt()
                f_mu /= f_var_diag_mean.sqrt().unsqueeze(-1)
                f_var_diag /= f_var_diag_mean.unsqueeze(-1)
            sum_exp = torch.exp(-f_mu).sum(dim=1).unsqueeze(-1)
            alpha = (1 - 2 / K + f_mu.exp() / K**2 * sum_exp) / f_var_diag
            return torch.nan_to_num(alpha / alpha.sum(dim=1).unsqueeze(-1), nan=1.0)

    def predictive_samples(self, f_mu, f_var, n_samples=100):
        """Sample from the posterior predictive on input data `x`.

        Parameters
        ----------
        f_mu : torch.Tensor
               posterior gp mean `(batch_size, output_shape)`
        f_var : torch.Tensor
                posterior gp var `(batch_size, output_shape, output_shape)`
        n_samples : int
            number of samples

        Returns
        -------
        samples : torch.Tensor
            samples `(n_samples, batch_size, output_shape)`
        """

        assert f_var.shape == torch.Size([f_mu.shape[0], f_mu.shape[1], f_mu.shape[1]])
        dist = MultivariateNormal(f_mu, f_var)
        samples = dist.sample((n_samples,))
        if self.likelihood == 'regression':
            return samples
        return torch.softmax(samples, dim=-1)

    @property
    def gp_kernel_prior_variance(self):
        return self._prior_factor_sod / self.prior_precision

    def functional_variance(self, Js_star):
        """
        GP posterior variance: \\( k_{**} - K_{*M} (K_{MM}+ L_{MM}^{-1})^{-1} K_{M*}\\)

        Parameters
        ----------
        Js_star : torch.Tensor of shape (N*, C, P)
                  Jacobians of test data points

        Returns
        -------
        f_var : torch.Tensor of shape (N*,C, C)
                Contains the posterior variances of N* testing points.
        """
        # Compute K_{**}
        K_star = self.gp_kernel_prior_variance * self._kernel_star(Js_star)

        # Compute K_{*M}
        K_M_star = []
        for X_batch, _ in self.train_loader:
            K_M_star_batch = self.gp_kernel_prior_variance * self._kernel_batch_star(
                Js_star, X_batch.to(self._device)
            )
            K_M_star.append(K_M_star_batch)
            del X_batch

        # Build_K_star_M computes K_{*M} (K_{MM}+ L_{MM}^{-1})^{-1} K_{M*}
        f_var = K_star - self._build_K_star_M(K_M_star)

        # If the considered kernel is diagonal, embed the covariances.
        # from (N*, C) -> (N*, C, C)
        if self.diagonal_kernel:
            f_var = torch.diag_embed(f_var)

        return f_var

    def functional_covariance(self, Js_star):
        """
        GP posterior covariance: \\( k_{**} - K_{*M} (K_{MM}+ L_{MM}^{-1})^{-1} K_{M*}\\)

        Parameters
        ----------
        Js_star : torch.Tensor of shape (N*, C, P)
                  Jacobians of test data points

        Returns
        -------
        f_var : torch.Tensor of shape (N*xC, N*xC)
                Contains the posterior covariances of N* testing points.
        """
        # Compute K_{**}
        K_star = self.gp_kernel_prior_variance * self._kernel_star(Js_star, joint=True)

        # Compute K_{*M}
        K_M_star = []
        for X_batch, _ in self.train_loader:
            K_M_star_batch = self.gp_kernel_prior_variance * self._kernel_batch_star(
                Js_star, X_batch.to(self._device)
            )
            K_M_star.append(K_M_star_batch)
            del X_batch

        # Build_K_star_M computes K_{*M} (K_{MM}+ L_{MM}^{-1})^{-1} K_{M*}
        f_var = K_star - self._build_K_star_M(K_M_star, joint=True)

        # If the considered kernel is diagonal, embed the covariances.
        # from (N*, N*, C) -> (N*, N*, C, C)
        if self.diagonal_kernel:
            f_var = torch.diag_embed(f_var)

        # Reshape from (N*, N*, C, C) to (N*xC, N*xC)
        f_var = f_var.permute(0, 2, 1, 3).flatten(0, 1).flatten(1, 2)

        return f_var

    def _build_K_star_M(self, K_M_star, joint=False):
        """
        Computes K_{*M} (K_{MM}+ L_{MM}^{-1})^{-1} K_{M*} given K_{M*}.

        Parameters
        ----------
        K_M_star : list of torch.Tensor
                   Contains K_{M*}. Tensors have shape (N_test, C, C)
                   or (N_test, C) for diagonal kernel.

        joint : boolean
                Wether to compute cross covariances or not.

        Returns
        -------
        torch.tensor of shape (N_test, N_test, C) for joint diagonal,
        (N_test, C) for non-joint diagonal, (N_test, N_test, C, C) for
        joint non-diagonal and (N_test, C, C) for non-joint non-diagonal.
        """
        # Shape (N_test, N, C, C) or (N_test, N, C) for diagonal
        K_M_star = torch.cat(K_M_star, dim=1)

        if self.diagonal_kernel:
            prods = []
            for c in range(self.n_outputs):
                # Compute K_{*M}L^{-1}
                v = torch.squeeze(
                    torch.linalg.solve(
                        self.Sigma_inv[c], K_M_star[:, :, c].unsqueeze(2)
                    ),
                    2,
                )
                if joint:
                    prod = torch.einsum('bm,am->ba', v, v)
                else:
                    prod = torch.einsum('bm,bm->b', v, v)
                prods.append(prod.unsqueeze(1))
            prods = torch.cat(prods, dim=-1)
            return prods
        else:
            # Reshape to (N_test, NxC, C) or (N_test, N, C)
            K_M_star = K_M_star.reshape(K_M_star.shape[0], -1, K_M_star.shape[-1])
            # Compute K_{*M}L^{-1}
            v = torch.linalg.solve(self.Sigma_inv, K_M_star)
            if joint:
                return torch.einsum('acm,bcn->abmn', v, v)
            else:
                return torch.einsum('bcm,bcn->bmn', v, v)

    @property
    def log_det_ratio(self):
        """
        Computes log determinant term in GP marginal likelihood

        For `classification` we use eq. (3.44) from Chapter 3.5 from
        [GP book R&W 2006](http://www.gaussianprocess.org/gpml/chapters/) with
        (note that we always use diagonal approximation \\(D\\) of the Hessian of log likelihood w.r.t. \\(f\\)):

        log determinant term := \\( \log | I + D^{1/2}K D^{1/2} | \\)

        For `regression`, we use ["standard" GP marginal likelihood](https://stats.stackexchange.com/questions/280105/log-marginal-likelihood-for-gaussian-process):

        log determinant term := \\( \log | K + \\sigma_2 I | \\)
        """
        if self.likelihood == 'regression':
            if self.diagonal_kernel:
                log_det = torch.tensor(0.0, requires_grad=True)
                for c in range(self.n_outputs):
                    log_det = log_det + torch.logdet(
                        self.gp_kernel_prior_variance * self.K_MM[c]
                        + torch.eye(n=self.K_MM[c].shape[0], device=self._device)
                        * self.sigma_noise.square()
                    )
                return log_det
            else:
                return torch.logdet(
                    self.gp_kernel_prior_variance * self.K_MM
                    + torch.eye(n=self.K_MM.shape[0], device=self._device)
                    * self.sigma_noise.square()
                )
        else:
            if self.diagonal_kernel:
                log_det = torch.tensor(0.0, requires_grad=True)
                for c in range(self.n_outputs):
                    W = torch.sqrt(self._H_factor * self.L[c])
                    log_det = log_det + torch.logdet(
                        W[:, None] * self.gp_kernel_prior_variance * self.K_MM[c] * W
                        + torch.eye(n=self.K_MM[c].shape[0], device=self._device)
                    )
                return log_det
            else:
                W = torch.sqrt(self._H_factor * self.L)
                return torch.logdet(
                    W[:, None] * self.gp_kernel_prior_variance * self.K_MM * W
                    + torch.eye(n=self.K_MM.shape[0], device=self._device)
                )

    @property
    def scatter(self, eps=0.00001):
        """
        Compute scatter term in GP log marginal likelihood.

        For `classification` we use eq. (3.44) from Chapter 3.5 from
        [GP book R&W 2006](http://www.gaussianprocess.org/gpml/chapters/) with \\(\hat{f} = f \\):

        scatter term := \\( f K^{-1} f^{T} \\)

        For `regression`, we use ["standard" GP marginal likelihood](https://stats.stackexchange.com/questions/280105/log-marginal-likelihood-for-gaussian-process):

        scatter term := \\( (y - m)K^{-1}(y -m )^T \\),
        where \\( m \\) is the mean of the GP prior, which in our case corresponds to
        \\( m := f + J (\\theta - \\theta_{MAP}) \\)

        """
        if self.likelihood == 'regression':
            noise = self.sigma_noise.square()
        else:
            noise = eps
        if self.diagonal_kernel:
            scatter = torch.tensor(0.0, requires_grad=True)
            for c in range(self.n_outputs):
                m = self.K_MM[c].shape[0]
                mu_term = torch.linalg.solve(
                    torch.linalg.cholesky(
                        self.gp_kernel_prior_variance * self.K_MM[c]
                        + torch.diag(torch.ones(m, device=self._device) * noise)
                    ),
                    self.mu[:, c],
                )
                scatter = scatter + torch.dot(mu_term, mu_term)
        else:
            m = self.K_MM.shape[0]
            mu_term = torch.linalg.solve(
                torch.linalg.cholesky(
                    self.gp_kernel_prior_variance * self.K_MM
                    + torch.diag(torch.ones(m, device=self._device) * noise)
                ),
                self.mu.reshape(-1),
            )
            scatter = torch.dot(mu_term, mu_term)
        return scatter

    def optimize_prior_precision(
        self,
        pred_type='gp',
        method='marglik',
        n_steps=100,
        lr=1e-1,
        init_prior_prec=1.0,
        prior_structure='scalar',
        val_loader=None,
        loss=None,
        log_prior_prec_min=-4,
        log_prior_prec_max=4,
        grid_size=100,
        link_approx='probit',
        n_samples=100,
        verbose=False,
        cv_loss_with_var=False,
        progress_bar=False,
    ):
        """
        `optimize_prior_precision_base` from `BaseLaplace` with `pred_type='gp'`
        """
        assert pred_type == 'gp'  # only gp supported
        assert prior_structure == 'scalar'  # only isotropic gaussian prior supported
        if method == 'marglik':
            warnings.warn(
                "Use of method='marglik' in case of FunctionalLaplace is discouraged, rather use method='CV'."
            )
        self.optimize_prior_precision_base(
            pred_type,
            method,
            n_steps,
            lr,
            init_prior_prec,
            prior_structure,
            val_loader,
            loss,
            log_prior_prec_min,
            log_prior_prec_max,
            grid_size,
            link_approx,
            n_samples,
            verbose,
            cv_loss_with_var,
            progress_bar,
        )
        self._build_Sigma_inv()

    def _kernel_batch(self, jacobians, batch):
        """
        Compute K_bb, which is part of K_MM kernel matrix.

        Parameters
        ----------
        jacobians : torch.Tensor (b, C, P)
        batch : torch.Tensor (b, C)

        Returns
        -------
        kernel : torch.tensor
            K_bb with shape (b * C, b * C)
        """
        jacobians_2, _ = self._jacobians(batch)
        P = jacobians.shape[-1]  # nr model params
        if self.diagonal_kernel:
            kernel = torch.empty(
                (jacobians.shape[0], jacobians_2.shape[0], self.n_outputs),
                device=jacobians.device,
            )
            for c in range(self.n_outputs):
                kernel[:, :, c] = torch.einsum(
                    'bp,ep->be', jacobians[:, c, :], jacobians_2[:, c, :]
                )
        else:
            kernel = torch.einsum(
                'ap,bp->ab', jacobians.reshape(-1, P), jacobians_2.reshape(-1, P)
            )
        del jacobians_2
        return kernel

    def _kernel_star(self, jacobians, joint=False):
        """
        Compute K_star_star kernel matrix.

        Parameters
        ----------
        jacobians : torch.Tensor (b, C, P)

        Returns
        -------
        kernel : torch.tensor
            K_star with shape (b, C, C)

        """
        if joint:
            if self.diagonal_kernel:
                kernel = torch.einsum('acp,bcp->abcc', jacobians, jacobians)
            else:
                kernel = torch.einsum('acp,bep->abce', jacobians, jacobians)

        else:
            if self.diagonal_kernel:
                kernel = torch.empty(
                    (jacobians.shape[0], self.n_outputs), device=jacobians.device
                )
                for c in range(self.n_outputs):
                    kernel[:, c] = torch.norm(jacobians[:, c, :], dim=1) ** 2
            else:
                kernel = torch.einsum('bcp,bep->bce', jacobians, jacobians)
        return kernel

    def _kernel_batch_star(self, jacobians, batch):
        """
        Compute K_b_star, which is a part of K_M_star kernel matrix.

        Parameters
        ----------
        jacobians : torch.Tensor (b1, C, P)
        batch : torch.Tensor (b2, C)

        Returns
        -------
        kernel : torch.tensor
            K_batch_star with shape (b1, b2, C, C)
        """
        jacobians_2, _ = self._jacobians(batch)
        if self.diagonal_kernel:
            kernel = torch.empty(
                (jacobians.shape[0], jacobians_2.shape[0], self.n_outputs),
                device=jacobians.device,
            )
            for c in range(self.n_outputs):
                kernel[:, :, c] = torch.einsum(
                    'bp,ep->be', jacobians[:, c, :], jacobians_2[:, c, :]
                )
        else:
            kernel = torch.einsum('bcp,dep->bdce', jacobians, jacobians_2)
        return kernel

    def _jacobians(self, X):
        """
        A wrapper function to compute jacobians - this enables reusing same kernel methods (kernel_batch etc.)
        in FunctionalLaplace and FunctionalLLLaplace by simply overwriting this method instead of all kernel methods.
        """
        return self.backend.jacobians(X, enable_backprop=self.enable_backprop)

    def _mean_scatter_term_batch(self, Js, f, y):
        """
        Compute mean vector in the scatter term in the log marginal likelihood

        See `scatter_lml` property above for the exact equations of mean vectors in scatter terms for
        both types of likelihood (regression, classification).

        Parameters
        ----------
        Js : torch.tensor
              Jacobians (batch, output_shape, parameters)
        f : torch.tensor
              NN output (batch, output_shape)
        y: torch.tensor
              data labels (batch, output_shape)

        Returns
        -------
        mu : torch.tensor
            K_batch_star with shape (batch, output_shape)
        """

        if self.likelihood == 'regression':
            return y - (
                f + torch.einsum('bcp,p->bc', Js, self.prior_mean - self.map_estimate)
            )
        elif self.likelihood == 'classification':
            return -torch.einsum('bcp,p->bc', Js, self.prior_mean - self.map_estimate)

    def log_marginal_likelihood(self, prior_precision=None, sigma_noise=None):
        """Compute the Laplace approximation to the log marginal likelihood.
        Requires that the Laplace approximation has been fit before.
        The resulting torch.Tensor is differentiable in `prior_precision` and
        `sigma_noise` if these have gradients enabled.
        By passing `prior_precision` or `sigma_noise`, the current value is
        overwritten. This is useful for iterating on the log marginal likelihood.

        Parameters
        ----------
        prior_precision : torch.Tensor, optional
            prior precision if should be changed from current `prior_precision` value
        sigma_noise : [type], optional
            observation noise standard deviation if should be changed

        Returns
        -------
        log_marglik : torch.Tensor
        """
        # update prior precision (useful when iterating on marglik)
        if prior_precision is not None:
            self.prior_precision = prior_precision

        # update sigma_noise (useful when iterating on marglik)
        if sigma_noise is not None:
            if self.likelihood != 'regression':
                raise ValueError('Can only change sigma_noise for regression.')
            self.sigma_noise = sigma_noise

        return self.log_likelihood - 0.5 * (self.log_det_ratio + self.scatter)

    @property
    def prior_precision(self):
        return self._prior_precision

    @prior_precision.setter
    def prior_precision(self, prior_precision):
        self._posterior_scale = None
        if np.isscalar(prior_precision) and np.isreal(prior_precision):
            self._prior_precision = torch.tensor([prior_precision], device=self._device)
        elif torch.is_tensor(prior_precision):
            if prior_precision.ndim == 0:
                # make dimensional
                self._prior_precision = prior_precision.reshape(-1).to(self._device)
            elif prior_precision.ndim == 1:
                if len(prior_precision) not in [1, self.n_layers, self.n_params]:
                    raise ValueError(
                        'Length of prior precision does not align with architecture.'
                    )
                self._prior_precision = prior_precision.to(self._device)
            else:
                raise ValueError(
                    'Prior precision needs to be at most one-dimensional tensor.'
                )
        else:
            raise ValueError(
                'Prior precision either scalar or torch.Tensor up to 1-dim.'
            )
        # This is a change from BaseLaplace. If the prior precision is changed, the cholesky
        #  decomposition needs to be recomputed.
        self._recompute_Sigma = True

    def state_dict(self) -> dict:
        state_dict = {
            'map_estimate': self.map_estimate,
            'M': self.M,
            'diagonal_kernel': self.diagonal_kernel,
            'seed': self.seed,
            'K_MM': self.K_MM,
            'Sigma_inv': self.Sigma_inv,
            '_prior_factor_sod': self._prior_factor_sod,
            '_fitted': self._fitted,
            '_recompute_Sigma': self._recompute_Sigma,
            'mu': self.mu,
            'L': self.L,
            'train_loader': self.train_loader,
            'loss': self.loss,
            'prior_mean': self.prior_mean,
            'prior_precision': self.prior_precision,
            'sigma_noise': self.sigma_noise,
            'n_data': self.n_data,
            'n_outputs': self.n_outputs,
            'likelihood': self.likelihood,
            'temperature': self.temperature,
            'enable_backprop': self.enable_backprop,
            'cls_name': self.__class__.__name__,
        }
        return state_dict

    def load_state_dict(self, state_dict: dict):
        # Dealbreaker errors
        if self.__class__.__name__ != state_dict['cls_name']:
            raise ValueError(
                'Loading a wrong Laplace type. Make sure `subset_of_weights` and'
                + ' `hessian_structure` are correct!'
            )
        if (
            self.n_params is not None
            and len(state_dict['map_estimate']) != self.n_params
        ):
            raise ValueError(
                'Attempting to load Laplace with different number of parameters than the model.'
                + ' Make sure that you use the same `subset_of_weights` value and the same `.requires_grad`'
                + ' switch on `model.parameters()`.'
            )
        if self.likelihood != state_dict['likelihood']:
            raise ValueError('Different likelihoods detected!')

        # Ignorable warnings
        if self.prior_mean is None and state_dict['prior_mean'] is not None:
            warnings.warn(
                'Loading non-`None` prior mean into a `None` prior mean. You might get wrong results.'
            )
        if self.temperature != state_dict['temperature']:
            warnings.warn(
                'Different `temperature` parameters detected. Some calculation might be off!'
            )
        if self.enable_backprop != state_dict['enable_backprop']:
            warnings.warn(
                'Different `enable_backprop` values. You might encounter error when differentiating'
                + ' the predictive mean and variance.'
            )

        self.map_estimate = state_dict['map_estimate']
        self.M = state_dict['M']
        self.diagonal_kernel = state_dict['diagonal_kernel']
        self.seed = state_dict['seed']
        self.K_MM = state_dict['K_MM']
        self.Sigma_inv = state_dict['Sigma_inv']
        self._prior_factor_sod = state_dict['_prior_factor_sod']
        self.mu = state_dict['mu']
        self.L = state_dict['L']
        self._fitted = state_dict['_fitted']
        self._recompute_Sigma = state_dict['_recompute_Sigma']
        self.train_loader = state_dict['train_loader']

        self.loss = state_dict['loss']
        self.prior_mean = state_dict['prior_mean']
        self.prior_precision = state_dict['prior_precision']
        self.sigma_noise = state_dict['sigma_noise']
        self.n_data = state_dict['n_data']
        self.n_outputs = state_dict['n_outputs']
        setattr(self.model, 'output_size', self.n_outputs)
        self.likelihood = state_dict['likelihood']
        self.temperature = state_dict['temperature']
        self.enable_backprop = state_dict['enable_backprop']
