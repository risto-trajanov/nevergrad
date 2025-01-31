# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import math
import warnings
import numpy as np
from pandas._config.config import options
from scipy import optimize as scipyoptimize
import nevergrad.common.typing as tp
from nevergrad.parametrization import parameter as p
from nevergrad.common import errors
from . import base
from .base import IntOrParameter
from . import recaster


class _ScipyMinimizeBase(recaster.SequentialRecastOptimizer):
    def __init__(
        self,
        parametrization: IntOrParameter,
        budget: tp.Optional[int] = None,
        num_workers: int = 1,
        *,
        method: str = "Nelder-Mead",
        random_restart: bool = False,
        options: dict = {},
        tol:float = 0
    ) -> None:
        super().__init__(parametrization, budget=budget, num_workers=num_workers)
        self.multirun = 1  # work in progress
        self.initial_guess: tp.Optional[tp.ArrayLike] = None
        # configuration
        assert method in ["Nelder-Mead", "COBYLA", "SLSQP", "Powell"], f"Unknown method '{method}'"
        self.method = method
        self.random_restart = random_restart
        self.options = options
        self.tol = tol

    def _internal_tell_not_asked(self, candidate: p.Parameter, loss: tp.Loss) -> None:
        """Called whenever calling "tell" on a candidate that was not "asked".
        Defaults to the standard tell pipeline.
        """  # We do not do anything; this just updates the current best.

    def get_optimization_function(self) -> tp.Callable[[tp.Callable[[tp.ArrayLike], float]], tp.ArrayLike]:
        # create a different sub-instance, so that the current instance is not referenced by the thread
        # (consequence: do not create a thread at initialization, or we get a thread explosion)
        subinstance = self.__class__(
            parametrization=self.parametrization,
            budget=self.budget,
            num_workers=self.num_workers,
            method=self.method,
            random_restart=self.random_restart,
            options=self.options,
            tol=self.tol
        )
        subinstance.archive = self.archive
        subinstance.current_bests = self.current_bests
        return subinstance._optimization_function

    def _optimization_function(self, objective_function: tp.Callable[[tp.ArrayLike], float]) -> tp.ArrayLike:
        # pylint:disable=unused-argument
        budget = np.inf if self.budget is None else self.budget
        best_res = np.inf
        best_x: np.ndarray = self.current_bests["average"].x  # np.zeros(self.dimension)
        if self.initial_guess is not None:
            best_x = np.array(self.initial_guess, copy=True)  # copy, just to make sure it is not modified
        remaining: float = budget - self._num_ask
        while remaining > 0:  # try to restart if budget is not elapsed
            options: tp.Dict[str, tp.Any] = self.options
            tol=self.tol
            res = scipyoptimize.minimize(
                objective_function,
                best_x if not self.random_restart else self._rng.normal(0.0, 1.0, self.dimension),
                method=self.method,
                options=options,
                tol=tol
            )
            if res.fun < best_res:
                best_res = res.fun
                best_x = res.x
            remaining = budget - self._num_ask
        return best_x


class ScipyOptimizer(base.ConfiguredOptimizer):
    """Wrapper over Scipy optimizer implementations, in standard ask and tell format.
    This is actually an import from scipy-optimize, including Sequential Quadratic Programming,

    Parameters
    ----------
    method: str
        Name of the method to use among:

        - Nelder-Mead
        - COBYLA
        - SQP (or SLSQP): very powerful e.g. in continuous noisy optimization. It is based on
          approximating the objective function by quadratic models.
        - Powell
    random_restart: bool
        whether to restart at a random point if the optimizer converged but the budget is not entirely
        spent yet (otherwise, restarts from best point)

    Note
    ----
    These optimizers do not support asking several candidates in a row
    """

    recast = True
    no_parallelization = True

    # pylint: disable=unused-argument
    def __init__(self, *, method: str = "Nelder-Mead", random_restart: bool = False, options:dict = {}, tol:float = 0) -> None:
        super().__init__(_ScipyMinimizeBase, locals())


NelderMead = ScipyOptimizer(method="Nelder-Mead").set_name("NelderMead", register=True)
Powell = ScipyOptimizer(method="Powell").set_name("Powell", register=True)
RPowell = ScipyOptimizer(method="Powell", random_restart=True).set_name("RPowell", register=True)
Cobyla = ScipyOptimizer(method="COBYLA").set_name("Cobyla", register=True)
RCobyla = ScipyOptimizer(method="COBYLA", random_restart=True).set_name("RCobyla", register=True)
SQP = ScipyOptimizer(method="SLSQP").set_name("SQP", register=True)
SLSQP = SQP  # Just so that people who are familiar with SLSQP naming are not lost.
RSQP = ScipyOptimizer(method="SLSQP", random_restart=True).set_name("RSQP", register=True)
RSLSQP = RSQP  # Just so that people who are familiar with SLSQP naming are not lost.


class _PymooMinimizeBase(recaster.SequentialRecastOptimizer):
    def __init__(
        self,
        parametrization: IntOrParameter,
        budget: tp.Optional[int] = None,
        num_workers: int = 1,
        *,
        algorithm: str,
    ) -> None:
        super().__init__(parametrization, budget=budget, num_workers=num_workers)
        # configuration
        self.algorithm = algorithm
        self._no_hypervolume = True

    def _internal_tell_not_asked(self, candidate: p.Parameter, loss: tp.Loss) -> None:
        """Called whenever calling "tell" on a candidate that was not "asked".
        Defaults to the standard tell pipeline.
        """  # We do not do anything; this just updates the current best.

    def get_optimization_function(self) -> tp.Callable[[tp.Callable[..., tp.Any]], tp.Optional[tp.ArrayLike]]:
        # create a different sub-instance, so that the current instance is not referenced by the thread
        # (consequence: do not create a thread at initialization, or we get a thread explosion)
        subinstance = self.__class__(
            parametrization=self.parametrization,
            budget=self.budget,
            num_workers=self.num_workers,
            algorithm=self.algorithm,
        )
        # set num_objectives in sub-instance for Pymoo to use in problem definition
        if self.num_objectives > 0:
            subinstance.num_objectives = self.num_objectives
        else:
            raise RuntimeError("num_objectives should have been set.")
        return subinstance._optimization_function
        # pylint:disable=useless-return

    def _optimization_function(
        self, objective_function: tp.Callable[[tp.ArrayLike], float]
    ) -> tp.Optional[tp.ArrayLike]:
        # pylint:disable=unused-argument, import-outside-toplevel
        from pymoo import optimize as pymoooptimize

        from pymoo.factory import get_algorithm as get_pymoo_algorithm

        # from pymoo.factory import get_reference_directions

        # reference direction code for when we want to use the other MOO optimizers in Pymoo
        # if self.algorithm in [
        #     "rnsga2",
        #     "nsga3",
        #     "unsga3",
        #     "rnsga3",
        #     "moead",
        #     "ctaea",
        # ]:  # algorithms that require reference points or reference directions
        #     the appropriate n_partitions must be looked into
        #     ref_dirs = get_reference_directions("das-dennis", self.num_objectives, n_partitions=12)
        #     algorithm = get_pymoo_algorithm(self.algorithm, ref_dirs)
        # else:
        algorithm = get_pymoo_algorithm(self.algorithm)
        problem = _create_pymoo_problem(self, objective_function)
        seed = self._rng.randint(2 ** 30)
        pymoooptimize.minimize(problem, algorithm, seed=seed)
        return None

    def _internal_ask_candidate(self) -> p.Parameter:
        """Reads messages from the thread in which the underlying optimization function is running
        New messages are sent as "ask".
        """
        # get a datapoint that is a random point in parameter space
        if self.num_objectives == 0:  # dummy ask i.e. not activating pymoo until num_objectives is set
            warnings.warn(
                "with this optimizer, it is more efficient to set num_objectives before the optimization begins",
                errors.NevergradRuntimeWarning,
            )
            return self.parametrization.spawn_child()
        return super()._internal_ask_candidate()

    def _internal_tell_candidate(self, candidate: p.Parameter, loss: float) -> None:
        """Returns value for a point which was "asked"
        (none asked point cannot be "tell")
        """
        if self._messaging_thread is None:
            return  # dummy tell i.e. not activating pymoo until num_objectives is set
        super()._internal_tell_candidate(candidate, loss)

    def _post_loss(self, candidate: p.Parameter, loss: float) -> tp.Loss:
        # pylint: disable=unused-argument
        """
        Multi-Objective override for this function.
        """
        return candidate.losses


class Pymoo(base.ConfiguredOptimizer):
    """Wrapper over Pymoo optimizer implementations, in standard ask and tell format.
    This is actually an import from Pymoo Optimize.

    Parameters
    ----------
    algorithm: str

        Use "algorithm-name" with following names to access algorithm classes:
        Single-Objective
        -"de"
        -'ga'
        -"brkga"
        -"nelder-mead"
        -"pattern-search"
        -"cmaes"
        Multi-Objective
        -"nsga2"
        Multi-Objective requiring reference directions, points or lines
        -"rnsga2"
        -"nsga3"
        -"unsga3"
        -"rnsga3"
        -"moead"
        -"ctaea"

    Note
    ----
    These optimizers do not support asking several candidates in a row
    """

    recast = True
    no_parallelization = True

    # pylint: disable=unused-argument
    def __init__(self, *, algorithm: str) -> None:
        super().__init__(_PymooMinimizeBase, locals())


class _PymooBatchMinimizeBase(recaster.BatchRecastOptimizer):
    def __init__(
        self,
        parametrization: IntOrParameter,
        budget: tp.Optional[int] = None,
        num_workers: int = 1,
        *,
        algorithm: str,
    ) -> None:
        super().__init__(parametrization, budget=budget, num_workers=num_workers)
        # configuration
        self.algorithm = algorithm
        self._no_hypervolume = True

    def _internal_tell_not_asked(self, candidate: p.Parameter, loss: tp.Loss) -> None:
        """Called whenever calling "tell" on a candidate that was not "asked".
        Defaults to the standard tell pipeline.
        """  # We do not do anything; this just updates the current best.

    def get_optimization_function(self) -> tp.Callable[[tp.Callable[..., tp.Any]], tp.Optional[tp.ArrayLike]]:
        # create a different sub-instance, so that the current instance is not referenced by the thread
        # (consequence: do not create a thread at initialization, or we get a thread explosion)
        subinstance = self.__class__(
            parametrization=self.parametrization,
            budget=self.budget,
            num_workers=self.num_workers,
            algorithm=self.algorithm,
        )
        # set num_objectives in sub-instance for Pymoo to use in problem definition
        if self.num_objectives <= 0:
            raise RuntimeError("num_objectives should have been set.")
        subinstance.num_objectives = self.num_objectives
        return subinstance._optimization_function
        # pylint:disable=useless-return

    def _optimization_function(
        self, objective_function: tp.Callable[[tp.ArrayLike], float]
    ) -> tp.Optional[tp.ArrayLike]:
        # pylint:disable=unused-argument, import-outside-toplevel
        from pymoo import optimize as pymoooptimize

        from pymoo.factory import get_algorithm as get_pymoo_algorithm

        # from pymoo.factory import get_reference_directions

        # reference direction code for when we want to use the other MOO optimizers in Pymoo
        # if self.algorithm in [
        #     "rnsga2",
        #     "nsga3",
        #     "unsga3",
        #     "rnsga3",
        #     "moead",
        #     "ctaea",
        # ]:  # algorithms that require reference points or reference directions
        #     the appropriate n_partitions must be looked into
        #     ref_dirs = get_reference_directions("das-dennis", self.num_objectives, n_partitions=12)
        #     algorithm = get_pymoo_algorithm(self.algorithm, ref_dirs)
        # else:
        algorithm = get_pymoo_algorithm(self.algorithm)
        problem = _create_pymoo_problem(self, objective_function, False)
        seed = self._rng.randint(2 ** 30)
        pymoooptimize.minimize(problem, algorithm, seed=seed)
        return None

    def _internal_ask_candidate(self) -> p.Parameter:
        """Reads messages from the thread in which the underlying optimization function is running
        New messages are sent as "ask".
        """
        # get a datapoint that is a random point in parameter space
        if self.num_objectives == 0:  # dummy ask i.e. not activating pymoo until num_objectives is set
            warnings.warn(
                "with this optimizer, it is more efficient to set num_objectives before the optimization begins",
                errors.NevergradRuntimeWarning,
            )
            return self.parametrization.spawn_child()
        return super()._internal_ask_candidate()

    def _internal_tell_candidate(self, candidate: p.Parameter, loss: float) -> None:
        """Returns value for a point which was "asked"
        (none asked point cannot be "tell")
        """
        if self._messaging_thread is None:
            return  # dummy tell i.e. not activating pymoo until num_objectives is set
        super()._internal_tell_candidate(candidate, loss)

    def _post_loss(self, candidate: p.Parameter, loss: float) -> tp.Loss:
        # pylint: disable=unused-argument
        """
        Multi-Objective override for this function.
        """
        return candidate.losses


class PymooBatch(base.ConfiguredOptimizer):
    """Wrapper over Pymoo optimizer implementations, in standard ask and tell format.
    This is actually an import from Pymoo Optimize.

    Parameters
    ----------
    algorithm: str

        Use "algorithm-name" with following names to access algorithm classes:
        Single-Objective
        -"de"
        -'ga'
        -"brkga"
        -"nelder-mead"
        -"pattern-search"
        -"cmaes"
        Multi-Objective
        -"nsga2"
        Multi-Objective requiring reference directions, points or lines
        -"rnsga2"
        -"nsga3"
        -"unsga3"
        -"rnsga3"
        -"moead"
        -"ctaea"

    Note
    ----
    These optimizers do not support asking several candidates in a row
    """

    recast = True

    # pylint: disable=unused-argument
    def __init__(self, *, algorithm: str) -> None:
        super().__init__(_PymooBatchMinimizeBase, locals())


def _create_pymoo_problem(
    optimizer: base.Optimizer,
    objective_function: tp.Callable[[tp.ArrayLike], float],
    elementwise: bool = True,
):
    # pylint:disable=import-outside-toplevel
    from pymoo.model.problem import Problem  # type: ignore

    class _PymooProblem(Problem):
        def __init__(self, optimizer, objective_function, elementwise):
            self.objective_function = objective_function
            super().__init__(
                n_var=optimizer.dimension,
                n_obj=optimizer.num_objectives,
                n_constr=0,  # constraints handled already by nevergrad
                xl=-math.pi * 0.5,
                xu=math.pi * 0.5,
                elementwise_evaluation=elementwise,
            )

        def _evaluate(self, X, out, *args, **kwargs):
            # pylint:disable=unused-argument
            # pymoo is supplying us with bounded parameters in [-pi/2,pi/2]. Nevergrad wants unbounded reals from us.
            out["F"] = self.objective_function(np.tan(X))

    return _PymooProblem(optimizer, objective_function, elementwise)


PymooNSGA2 = Pymoo(algorithm="nsga2").set_name(
    "PymooNSGA2", register=False
)  # , register=True)   temporarily removed!
PymooBatchNSGA2 = PymooBatch(algorithm="nsga2").set_name("PymooBatchNSGA2", register=False)
