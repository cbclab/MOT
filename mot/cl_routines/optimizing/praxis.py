from ...cl_functions import PrAxisFunc
from .base import AbstractParallelOptimizer, AbstractParallelOptimizerWorker

__author__ = 'Robbert Harms'
__date__ = "2014-02-05"
__license__ = "LGPL v3"
__maintainer__ = "Robbert Harms"
__email__ = "robbert.harms@maastrichtuniversity.nl"


class PrAxis(AbstractParallelOptimizer):

    default_patience = 1000

    def __init__(self, cl_environments, load_balancer, use_param_codec=True, patience=None,
                 optimizer_options=None, **kwargs):
        """Use the Principal Axis method to calculate the optimum.

        This uses the Principal Axis implementation from NLOpt, slightly adapted for use in MOT.

        Args:
            patience (int):
                Used to set the maximum number of iterations to patience*(number_of_parameters+1)
            optimizer_options (dict): the optimization settings, for the defaults please see PrAxisFunc.
        """
        patience = patience or self.default_patience
        super(PrAxis, self).__init__(cl_environments, load_balancer, use_param_codec, patience=patience,
                                     optimizer_options=optimizer_options, **kwargs)

    def _get_worker_class(self):
        return PrAxisWorker


class PrAxisWorker(AbstractParallelOptimizerWorker):

    def _get_optimization_function(self):
        return PrAxisFunc(self._nmr_params, patience=self._parent_optimizer.patience,
                          optimizer_options=self._optimizer_options)

    def _get_optimizer_call_name(self):
        return 'praxis'

    def _optimizer_supports_float(self):
        return True

    def _optimizer_supports_double(self):
        return True

    def _uses_random_numbers(self):
        return True