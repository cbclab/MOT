import logging
from ...cl_routines.filters.median import MedianFilter
from ...cl_routines.optimizing.base import AbstractOptimizer
from ...cl_routines.mapping.error_measures import ErrorMeasures
from ...cl_routines.mapping.residual_calculator import ResidualCalculator
from ...cl_routines.optimizing.nmsimplex import NMSimplex

__author__ = 'Robbert Harms'
__date__ = "2014-06-19"
__license__ = "LGPL v3"
__maintainer__ = "Robbert Harms"
__email__ = "robbert.harms@maastrichtuniversity.nl"


class MetaOptimizer(AbstractOptimizer):

    def __init__(self, cl_environments, load_balancer, use_param_codec=True, patience=None):
        """This meta optimization routine uses optimizers and smoothing routines to provide a meta optimization.

        In general one can enable a grid search beforehand, multiple optimizers, parameter smoothing and perturbation.

        It will also calculating the error maps for the final fitted model parameters.

        Args:
            cl_environments (list of CLEnvironment): a list with the cl environments to use
            load_balancer (LoadBalancer): the load balance strategy to use
            use_param_codec (boolean): if this minimization should use the parameter codecs (param transformations)
            patience (int): The patience is used in the calculation of how many iterations to iterate the optimizer.
                The exact semantical value of this parameter may change per optimizer.

        Attributes:
            extra_optim_runs (boolean, default 1): The amount of extra optimization runs with a smoothing step
                in between.
            extra_optim_runs_optimizers (list, default None): A list of optimizers with one optimizer for every extra
                optimization run. If the length of this list is smaller than the number of runs, the last optimizer is
                used for all remaining runs.
            extra_optim_runs_smoothers (list, default None): A list of smoothers with one smoother for every extra
                optimization run. If the length of this list is smaller than the number of runs, the last smoother is
                used for all remaining runs.
            extra_optim_runs_apply_smoothing (boolen): If we want to use smoothing or not. This is mutually exclusive
                with extra_optim_runs_use_perturbation.
            extra_optim_runs_use_perturbation (boolean): If we want to use the parameter perturbation by the model
                or not. This is mutually exclusive with extra_optim_runs_apply_smoothing.
            optimizer (Optimizer, default NMSimplex): The default optimization routine
            smoother (Smoother, default MedianFilter(1)): The default smoothing routine
        """
        super(MetaOptimizer, self).__init__(cl_environments, load_balancer, use_param_codec)
        self.enable_sampling = False

        self.extra_optim_runs = 0
        self.extra_optim_runs_optimizers = None
        self.extra_optim_runs_smoothers = None
        self.extra_optim_runs_apply_smoothing = False
        self.extra_optim_runs_use_perturbation = True

        self.optimizer = NMSimplex(self.cl_environments, self.load_balancer, use_param_codec=self.use_param_codec,
                                   patience=patience)
        self.smoother = MedianFilter((1, 1, 1), self.cl_environments, self.load_balancer)

        self._propagate_property('cl_environments', cl_environments)
        self._propagate_property('load_balancer', load_balancer)

        self._logger = logging.getLogger(__name__)

    def minimize(self, model, init_params=None, full_output=False):
        results = init_params

        results = self.optimizer.minimize(model, init_params=results)

        if self.extra_optim_runs:
            for i in range(self.extra_optim_runs):
                optimizer = self.optimizer
                smoother = self.smoother

                if self.extra_optim_runs_optimizers and i < len(self.extra_optim_runs_optimizers):
                    optimizer = self.extra_optim_runs_optimizers[i]

                if self.extra_optim_runs_apply_smoothing:
                    if self.extra_optim_runs_smoothers and i < len(self.extra_optim_runs_smoothers):
                        smoother = self.extra_optim_runs_smoothers[i]
                    smoothed_maps = model.smooth(results, smoother)
                    results = optimizer.minimize(model, init_params=smoothed_maps)

                elif self.extra_optim_runs_use_perturbation:
                    perturbed_params = model.perturbate(results)
                    results = optimizer.minimize(model, init_params=perturbed_params)
                else:
                    results = optimizer.minimize(model, init_params=results)

        errors = ResidualCalculator(cl_environments=self.cl_environments,
                                    load_balancer=self.load_balancer).calculate(model, results)
        error_measures = ErrorMeasures(self.cl_environments, self.load_balancer,
                                       model.double_precision).calculate(errors)
        results.update(error_measures)

        if full_output:
            return results, {}
        return results

    @property
    def cl_environments(self):
        return self._cl_environments

    @property
    def load_balancer(self):
        return self._load_balancer

    @cl_environments.setter
    def cl_environments(self, cl_environments):
        self._propagate_property('cl_environments', cl_environments)
        self._cl_environments = cl_environments

    @load_balancer.setter
    def load_balancer(self, load_balancer):
        self._propagate_property('load_balancer', load_balancer)
        self._load_balancer = load_balancer

    def _propagate_property(self, name, value):
        self.optimizer.__setattr__(name, value)
        self.smoother.__setattr__(name, value)

        if self.extra_optim_runs_optimizers:
            for optim in self.extra_optim_runs_optimizers:
                optim.__setattr__(name, value)

        if self.extra_optim_runs_smoothers:
            for smoother in self.extra_optim_runs_smoothers:
                smoother.__setattr__(name, value)