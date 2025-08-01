# copyright: sktime developers, BSD-3-Clause License (see LICENSE file)
"""Suite of tests for all estimators.

adapted from scikit-learn's estimator_checks
"""

__author__ = ["mloning", "fkiraly", "achieveordie"]

import numbers
import os
import types
from copy import deepcopy
from inspect import getfullargspec, isclass, signature
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
import pytest
from _pytest.outcomes import Skipped

from sktime.base import BaseEstimator, BaseObject, load
from sktime.classification.deep_learning.base import BaseDeepClassifier
from sktime.dists_kernels.base import (
    BasePairwiseTransformer,
    BasePairwiseTransformerPanel,
)
from sktime.exceptions import NotFittedError
from sktime.forecasting.base import BaseForecaster
from sktime.registry import all_estimators, get_base_class_lookup, scitype
from sktime.regression.deep_learning.base import BaseDeepRegressor
from sktime.tests._config import (
    CYTHON_ESTIMATORS,
    EXCLUDE_ESTIMATORS,
    EXCLUDED_TESTS,
    MATRIXDESIGN,
    NON_STATE_CHANGING_METHODS,
    NON_STATE_CHANGING_METHODS_ARRAYLIKE,
    VALID_ESTIMATOR_TAGS,
)
from sktime.tests.test_switch import run_test_for_class
from sktime.utils._testing._conditional_fixtures import (
    create_conditional_fixtures_and_names,
)
from sktime.utils._testing.estimator_checks import (
    _assert_array_almost_equal,
    _assert_array_equal,
    _get_args,
    _has_capability,
    _list_required_methods,
)
from sktime.utils._testing.scenarios_getter import retrieve_scenarios
from sktime.utils.deep_equals import deep_equals
from sktime.utils.dependencies import _check_soft_dependencies
from sktime.utils.random_state import set_random_state
from sktime.utils.sampling import random_partition


def subsample_by_version_os(x):
    """Subsample objects by operating system and python version.

    Ensures each estimator is tested at least once on every OS and python version, if
    combined with a matrix of OS/versions.

    Currently assumes that matrix includes py3.8-3.10, and win/ubuntu/mac.
    """
    import platform
    import sys

    ix = sys.version_info.minor % 3
    os_str = platform.system()
    if os_str == "Windows":
        ix = ix
    elif os_str == "Linux":
        ix = ix + 1
    elif os_str == "Darwin":
        ix = ix + 2
    else:
        raise ValueError(f"found unexpected OS string: {os_str}")
    ix = ix % 3

    part = random_partition(len(x), 3)
    subset_idx = part[ix]
    res = [x[i] for i in subset_idx]

    return res


class ValidProbaErrors:
    """Context manager, returns None on valid predict_proba or skpro exception."""

    def __enter__(self):
        self.skipped = False
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if isinstance(exc_value, NotImplementedError):
            self.skipped = True
            return True
        if isinstance(exc_value, ImportError) and "skpro" in str(exc_value):
            self.skipped = True
            return True
        return False  # Propagate any other exceptions


class BaseFixtureGenerator:
    """Fixture generator for base testing functionality in sktime.

    Test classes inheriting from this and not overriding pytest_generate_tests
        will have estimator and scenario fixtures parametrized out of the box.

    Descendants can override:
        estimator_type_filter: str, class variable; None or scitype string
            e.g., "forecaster", "transformer", "classifier", see BASE_CLASS_SCITYPE_LIST
            which estimators are being retrieved and tested
        fixture_sequence: list of str
            sequence of fixture variable names in conditional fixture generation
        _generate_[variable]: object methods, all (test_name: str, **kwargs) -> list
            generating list of fixtures for fixture variable with name [variable]
                to be used in test with name test_name
            can optionally use values for fixtures earlier in fixture_sequence,
                these must be input as kwargs in a call
        is_excluded: static method (test_name: str, est: class) -> bool
            whether test with name test_name should be excluded for estimator est
                should be used only for encoding general rules, not individual skips
                individual skips should go on the EXCLUDED_TESTS list in _config
            requires _generate_estimator_class and _generate_estimator_instance as is
        _excluded_scenario: static method (test_name: str, scenario) -> bool
            whether scenario should be skipped in test with test_name test_name
            requires _generate_estimator_scenario as is

    Fixtures parametrized
    ---------------------
    estimator_class: estimator inheriting from BaseObject
        ranges over estimator classes not excluded by EXCLUDE_ESTIMATORS, EXCLUDED_TESTS
    estimator_instance: instance of estimator inheriting from BaseObject
        ranges over estimator classes not excluded by EXCLUDE_ESTIMATORS, EXCLUDED_TESTS
        instances are generated by create_test_instance class method of estimator_class
    scenario: instance of TestScenario
        ranges over all scenarios returned by retrieve_scenarios
        applicable for estimator_class or estimator_instance
    method_nsc: string, name of estimator method
        ranges over all "predict"-like, non-state-changing methods
        of estimator_instance or estimator_class that the class/object implements
    method_nsc_arraylike: string, for non-state-changing estimator methods
        ranges over all "predict"-like, non-state-changing estimator methods,
        which return an array-like output
    """

    # class variables which can be overridden by descendants

    # which estimator types are generated; None=all, or scitype string like "forecaster"
    estimator_type_filter = None

    # which sequence the conditional fixtures are generated in
    fixture_sequence = [
        "estimator_class",
        "estimator_instance",
        "scenario",
        "method_nsc",
        "method_nsc_arraylike",
    ]

    # which fixtures are indirect, e.g., have an additional pytest.fixture block
    #   to generate an indirect fixture at runtime. Example: estimator_instance
    #   warning: direct fixtures retain state changes within the same test
    indirect_fixtures = ["estimator_instance"]

    def pytest_generate_tests(self, metafunc):
        """Test parameterization routine for pytest.

        This uses create_conditional_fixtures_and_names and generator_dict to create the
        fixtures for a mark.parametrize decoration of all tests.
        """
        # get name of the test
        test_name = metafunc.function.__name__

        fixture_sequence = self.fixture_sequence

        fixture_vars = getfullargspec(metafunc.function)[0]

        (
            fixture_param_str,
            fixture_prod,
            fixture_names,
        ) = create_conditional_fixtures_and_names(
            test_name=test_name,
            fixture_vars=fixture_vars,
            generator_dict=self.generator_dict(),
            fixture_sequence=fixture_sequence,
            raise_exceptions=True,
        )

        # determine indirect variables for the parametrization block
        #   this is intersection of self.indirect_vixtures with args in fixture_vars
        indirect_vars = list(set(fixture_vars).intersection(self.indirect_fixtures))

        metafunc.parametrize(
            fixture_param_str,
            fixture_prod,
            ids=fixture_names,
            indirect=indirect_vars,
        )

    def _all_estimators(self):
        """Retrieve list of all estimator classes of type self.estimator_type_filter."""
        # TODO(fangelim): refactor this _all_estimators
        # to make it possible to set custom tags to filter
        # as class attributes, similar to `estimator_type_filter`
        if CYTHON_ESTIMATORS:
            filter_tags = {"requires_cython": True, "tests:skip_all": False}
        else:
            filter_tags = {"tests:skip_all": False}

        est_list = all_estimators(
            estimator_types=getattr(self, "estimator_type_filter", None),
            return_names=False,
            exclude_estimators=EXCLUDE_ESTIMATORS,
            filter_tags=filter_tags,
        )
        # subsample estimators by OS & python version
        # this ensures that only a 1/3 of estimators are tested for a given combination
        # but all are tested on every OS at least once, and on every python version once
        if MATRIXDESIGN:
            est_list = subsample_by_version_os(est_list)

        # run_test_for_class selects the estimators to run
        # based on whether they have changed, and whether they have all dependencies
        # internally, uses the ONLY_CHANGED_MODULES flag,
        # and checks the python env against python_dependencies tag
        est_list = [est for est in est_list if run_test_for_class(est)]

        return est_list

    def generator_dict(self):
        """Return dict with methods _generate_[variable] collected in a dict.

        The returned dict is the one required by create_conditional_fixtures_and_names,
            used in this _conditional_fixture plug-in to pytest_generate_tests, above.

        Returns
        -------
        generator_dict : dict, with keys [variable], where
            [variable] are all strings such that self has a static method
                named _generate_[variable](test_name: str, **kwargs)
            value at [variable] is a reference to _generate_[variable]
        """
        gens = [attr for attr in dir(self) if attr.startswith("_generate_")]
        vars = [gen.replace("_generate_", "") for gen in gens]

        generator_dict = dict()
        for var, gen in zip(vars, gens):
            generator_dict[var] = getattr(self, gen)

        return generator_dict

    @staticmethod
    def is_excluded(test_name, est):
        """Shorthand to check whether test test_name is excluded for estimator est."""
        # there are two conditions for exclusion:
        # 1. the estimator is excluded in the legacy EXCLUDED_TESTS list
        # 2. the excluded test appears in the "tests:skip_by_name" tag
        cond1 = test_name in EXCLUDED_TESTS.get(est.__name__, [])
        excl_tag = est.get_class_tag("tests:skip_by_name", [])
        if excl_tag is None:
            excl_tag = []
        cond2 = test_name in excl_tag
        excluded = cond1 or cond2
        return excluded

    # the following functions define fixture generation logic for pytest_generate_tests
    # each function is of signature (test_name:str, **kwargs) -> List of fixtures
    # function with name _generate_[fixture_var] returns list of values for fixture_var
    #   where fixture_var is a fixture variable used in tests
    # the list is conditional on values of other fixtures which can be passed in kwargs

    def _generate_estimator_class(self, test_name, **kwargs):
        """Return estimator class fixtures.

        Fixtures parametrized
        ---------------------
        estimator_class: estimator inheriting from BaseObject
            ranges over all estimator classes not excluded by EXCLUDED_TESTS
        """
        estimator_classes_to_test = [
            est
            for est in self._all_estimators()
            if not self.is_excluded(test_name, est)
        ]

        estimator_names = [est.__name__ for est in estimator_classes_to_test]

        return estimator_classes_to_test, estimator_names

    def _generate_estimator_instance(self, test_name, **kwargs):
        """Return estimator instance fixtures.

        Fixtures parametrized
        ---------------------
        estimator_instance: instance of estimator inheriting from BaseObject
            ranges over all estimator classes not excluded by EXCLUDED_TESTS
            instances are generated by create_test_instance class method
        """
        # call _generate_estimator_class to get all the classes
        estimator_classes_to_test, _ = self._generate_estimator_class(
            test_name=test_name
        )

        # create instances from the classes
        estimator_instances_to_test = []
        estimator_instance_names = []
        # retrieve all estimator parameters if multiple, construct instances
        for est in estimator_classes_to_test:
            all_instances_of_est, instance_names = est.create_test_instances_and_names()
            estimator_instances_to_test += all_instances_of_est
            estimator_instance_names += instance_names

        return estimator_instances_to_test, estimator_instance_names

    # this is executed before each test instance call
    #   if this were not executed, estimator_instance would keep state changes
    #   within executions of the same test with different parameters
    @pytest.fixture(scope="function")
    def estimator_instance(self, request):
        """estimator_instance fixture definition for indirect use."""
        # esetimator_instance is cloned at the start of every test
        return request.param.clone()

    def _generate_scenario(self, test_name, **kwargs):
        """Return estimator test scenario.

        Fixtures parametrized
        ---------------------
        scenario: instance of TestScenario
            ranges over all scenarios returned by retrieve_scenarios
        """
        if "estimator_class" in kwargs.keys():
            obj = kwargs["estimator_class"]
        elif "estimator_instance" in kwargs.keys():
            obj = kwargs["estimator_instance"]
        else:
            return []

        scenarios = retrieve_scenarios(obj)
        scenarios = [s for s in scenarios if not self._excluded_scenario(test_name, s)]
        scenario_names = [type(scen).__name__ for scen in scenarios]

        return scenarios, scenario_names

    @staticmethod
    def _excluded_scenario(test_name, scenario):
        """Skip list generator for scenarios to skip in test_name.

        Arguments
        ---------
        test_name : str, name of test
        scenario : instance of TestScenario, to be used in test

        Returns
        -------
        bool, whether scenario should be skipped in test_name
        """
        # for forecasters tested in test_methods_do_not_change_state
        #   if fh is not passed in fit, then this test would fail
        #   since fh will be stored in predict through fh handling
        #   as there are scenarios which pass it early and everything else is the same
        #   we skip those scenarios
        if test_name == "test_methods_do_not_change_state":
            if not scenario.get_tag("fh_passed_in_fit", True, raise_error=False):
                return True

        # this line excludes all scenarios that do not have "is_enabled" flag
        #   we should slowly enable more scenarios for better coverage
        # comment out to run the full test suite with new scenarios
        if not scenario.get_tag("is_enabled", False, raise_error=False):
            return True

        return False

    def _generate_method_nsc(self, test_name, **kwargs):
        """Return estimator test scenario.

        Fixtures parametrized
        ---------------------
        method_nsc: string, for non-state-changing estimator methods
            ranges over all "predict"-like, non-state-changing estimator methods
        """
        # ensure cls is a class
        if "estimator_class" in kwargs.keys():
            obj = kwargs["estimator_class"]
        elif "estimator_instance" in kwargs.keys():
            obj = kwargs["estimator_instance"]
        else:
            return []

        # complete list of all non-state-changing methods
        nsc_list = NON_STATE_CHANGING_METHODS

        # subset to the methods that x has implemented
        nsc_list = [x for x in nsc_list if _has_capability(obj, x)]

        return nsc_list

    def _generate_method_nsc_arraylike(self, test_name, **kwargs):
        """Return estimator test scenario.

        Fixtures parametrized
        ---------------------
        method_nsc_arraylike: string, for non-state-changing estimator methods
            ranges over all "predict"-like, non-state-changing estimator methods,
            which return an array-like output
        """
        method_nsc_list = self._generate_method_nsc(test_name=test_name, **kwargs)

        # subset to the arraylike ones to avoid copy-paste
        nsc_list_arraylike = set(method_nsc_list).intersection(
            NON_STATE_CHANGING_METHODS_ARRAYLIKE
        )
        return list(nsc_list_arraylike)


class QuickTester:
    """Mixin class which adds the run_tests method to run tests on one estimator."""

    def run_tests(
        self,
        estimator,
        raise_exceptions=False,
        tests_to_run=None,
        fixtures_to_run=None,
        tests_to_exclude=None,
        fixtures_to_exclude=None,
        verbose=False,
    ):
        """Run all tests on one single estimator.

        All tests in self are run on the following estimator type fixtures:
            if est is a class, then estimator_class = est, and
                estimator_instance loops over est.create_test_instance()
            if est is an object, then estimator_class = est.__class__, and
                estimator_instance = est

        This is compatible with pytest.mark.parametrize decoration,
            but currently only with multiple *single variable* annotations.

        Parameters
        ----------
        estimator : estimator class or estimator instance

        raise_exceptions : bool, optional, default=False
            whether to return exceptions/failures in the results dict, or raise them

            * if False: returns exceptions in returned `results` dict
            * if True: raises exceptions as they occur

        tests_to_run : str or list of str, names of tests to run. default = all tests
            sub-sets tests that are run to the tests given here.

        fixtures_to_run : str or list of str, pytest test-fixture combination codes.
            which test-fixture combinations to run. Default = run all of them.
            sub-sets tests and fixtures to run to the list given here.
            If both tests_to_run and fixtures_to_run are provided, runs the *union*,
            i.e., all test-fixture combinations for tests in tests_to_run,
            plus all test-fixture combinations in fixtures_to_run.

        tests_to_exclude : str or list of str, names of tests to exclude. default = None
            removes tests that should not be run, after subsetting via tests_to_run.

        fixtures_to_exclude : str or list of str, fixtures to exclude. default = None
            removes test-fixture combinations that should not be run.
            This is done after subsetting via fixtures_to_run.

        verbose : bool, optional, default=False
            whether to print the results of the tests as they are run

        Returns
        -------
        results : dict of results of the tests in self
            keys are test/fixture strings, identical as in pytest, e.g., test[fixture]
            entries are the string "PASSED" if the test passed,
            or the exception raised if the test did not pass
            returned only if all tests pass,
            or raise_exceptions=False

        Raises
        ------
        if raise_exceptions=True,
        raises any exception produced by the tests directly

        Examples
        --------
        >>> from sktime.forecasting.naive import NaiveForecaster
        >>> from sktime.tests.test_all_estimators import TestAllObjects
        >>> TestAllObjects().run_tests(
        ...     NaiveForecaster,
        ...     tests_to_run="test_constructor"
        ... )
        {'test_constructor[NaiveForecaster]': 'PASSED'}
        >>> TestAllObjects().run_tests(
        ...     NaiveForecaster, fixtures_to_run="test_repr[NaiveForecaster-2]"
        ... )
        {'test_repr[NaiveForecaster-2]': 'PASSED'}
        """
        tests_to_run = self._check_None_str_or_list_of_str(
            tests_to_run, var_name="tests_to_run"
        )
        fixtures_to_run = self._check_None_str_or_list_of_str(
            fixtures_to_run, var_name="fixtures_to_run"
        )
        tests_to_exclude = self._check_None_str_or_list_of_str(
            tests_to_exclude, var_name="tests_to_exclude"
        )
        fixtures_to_exclude = self._check_None_str_or_list_of_str(
            fixtures_to_exclude, var_name="fixtures_to_exclude"
        )

        # retrieve tests from self
        test_names = [attr for attr in dir(self) if attr.startswith("test")]

        # we override the generator_dict, by replacing it with temp_generator_dict:
        #  the only estimator (class or instance) is est, this is overridden
        #  the remaining fixtures are generated conditionally, without change
        temp_generator_dict = deepcopy(self.generator_dict())

        if isclass(estimator):
            estimator_class = estimator
        else:
            estimator_class = type(estimator)

        def _generate_estimator_class(test_name, **kwargs):
            return [estimator_class], [estimator_class.__name__]

        def _generate_estimator_instance(test_name, **kwargs):
            return [estimator.clone()], [estimator_class.__name__]

        def _generate_estimator_instance_cls(test_name, **kwargs):
            return estimator_class.create_test_instances_and_names()

        temp_generator_dict["estimator_class"] = _generate_estimator_class

        if not isclass(estimator):
            temp_generator_dict["estimator_instance"] = _generate_estimator_instance
        else:
            temp_generator_dict["estimator_instance"] = _generate_estimator_instance_cls
        # override of generator_dict end, temp_generator_dict is now prepared

        # sub-setting to specific tests to run, if tests or fixtures were specified
        if tests_to_run is None and fixtures_to_run is None:
            test_names_subset = test_names
        else:
            test_names_subset = []
            if tests_to_run is not None:
                test_names_subset += list(set(test_names).intersection(tests_to_run))
            if fixtures_to_run is not None:
                # fixture codes contain the test as substring until the first "["
                tests_from_fixt = [fixt.split("[")[0] for fixt in fixtures_to_run]
                test_names_subset += list(set(test_names).intersection(tests_from_fixt))
            test_names_subset = list(set(test_names_subset))

        # sub-setting by removing all tests from tests_to_exclude
        if tests_to_exclude is not None:
            test_names_subset = list(
                set(test_names_subset).difference(tests_to_exclude)
            )

        # the below loops run all the tests and collect the results here:
        results = dict()
        # loop A: we loop over all the tests
        for test_name in test_names_subset:
            test_fun = getattr(self, test_name)
            fixture_sequence = self.fixture_sequence

            # all arguments except the first one (self)
            fixture_vars = getfullargspec(test_fun)[0][1:]
            fixture_vars = [var for var in fixture_sequence if var in fixture_vars]

            # this call retrieves the conditional fixtures
            #  for the test test_name, and the estimator
            _, fixture_prod, fixture_names = create_conditional_fixtures_and_names(
                test_name=test_name,
                fixture_vars=fixture_vars,
                generator_dict=temp_generator_dict,
                fixture_sequence=fixture_sequence,
                raise_exceptions=raise_exceptions,
            )

            # if function is decorated with mark.parametrize, add variable settings
            # NOTE: currently this works only with single-variable mark.parametrize
            if hasattr(test_fun, "pytestmark"):
                if len([x for x in test_fun.pytestmark if x.name == "parametrize"]) > 0:
                    # get the three lists from pytest
                    (
                        pytest_fixture_vars,
                        pytest_fixture_prod,
                        pytest_fixture_names,
                    ) = self._get_pytest_mark_args(test_fun)
                    # add them to the three lists from conditional fixtures
                    fixture_vars, fixture_prod, fixture_names = self._product_fixtures(
                        fixture_vars,
                        fixture_prod,
                        fixture_names,
                        pytest_fixture_vars,
                        pytest_fixture_prod,
                        pytest_fixture_names,
                    )

            def print_if_verbose(msg):
                if verbose:
                    print(msg)  # noqa: T001, T201

            # loop B: for each test, we loop over all fixtures
            for params, fixt_name in zip(fixture_prod, fixture_names):
                # this is needed because pytest unwraps 1-tuples automatically
                # but subsequent code assumes params is k-tuple, no matter what k is
                if len(fixture_vars) == 1:
                    params = (params,)
                key = f"{test_name}[{fixt_name}]"
                args = dict(zip(fixture_vars, params))

                # we subset to test-fixtures to run by this, if given
                #  key is identical to the pytest test-fixture string identifier
                if fixtures_to_run is not None and key not in fixtures_to_run:
                    continue
                if fixtures_to_exclude is not None and key in fixtures_to_exclude:
                    continue

                print_if_verbose(f"{key}")

                try:
                    test_fun(**deepcopy(args))
                    results[key] = "PASSED"
                    print_if_verbose("PASSED")
                except Skipped as err:
                    results[key] = f"SKIPPED: {err.msg}"
                    print_if_verbose(f"SKIPPED: {err.msg}")
                except Exception as err:
                    results[key] = err
                    print_if_verbose(f"FAILED: {err}")
                    if raise_exceptions:
                        raise err

        return results

    @staticmethod
    def _check_None_str_or_list_of_str(obj, var_name="obj"):
        """Check that obj is None, str, or list of str, and coerce to list of str."""
        if obj is not None:
            msg = f"{var_name} must be None, str, or list of str"
            if isinstance(obj, str):
                obj = [obj]
            if not isinstance(obj, list):
                raise ValueError(msg)
            if not np.all([isinstance(x, str) for x in obj]):
                raise ValueError(msg)
        return obj

    # todo: surely there is a pytest method that can be called instead of this?
    #   find and replace if it exists
    @staticmethod
    def _get_pytest_mark_args(fun):
        """Get args from pytest mark annotation of function.

        Parameters
        ----------
        fun: callable, any function

        Returns
        -------
        pytest_fixture_vars: list of str
            names of args participating in mark.parametrize marks, in pytest order
        pytest_fixt_list: list of tuple
            list of value tuples from the mark parameterization
            i-th value in each tuple corresponds to i-th arg name in pytest_fixture_vars
        pytest_fixt_names: list of str
            i-th element is display name for i-th fixture setting in pytest_fixt_list
        """
        from itertools import product

        marks = [x for x in fun.pytestmark if x.name == "parametrize"]

        def to_str(obj):
            return [str(x) for x in obj]

        def get_id(mark):
            if "ids" in mark.kwargs.keys():
                return mark.kwargs["ids"]
            else:
                return to_str(range(len(mark.args[1])))

        pytest_fixture_vars = [x.args[0] for x in marks]
        pytest_fixt_raw = [x.args[1] for x in marks]
        pytest_fixt_list = product(*pytest_fixt_raw)
        pytest_fixt_names_raw = [get_id(x) for x in marks]
        pytest_fixt_names = product(*pytest_fixt_names_raw)
        pytest_fixt_names = ["-".join(x) for x in pytest_fixt_names]

        return pytest_fixture_vars, pytest_fixt_list, pytest_fixt_names

    @staticmethod
    def _product_fixtures(
        fixture_vars,
        fixture_prod,
        fixture_names,
        pytest_fixture_vars,
        pytest_fixture_prod,
        pytest_fixture_names,
    ):
        """Compute products of two sets of fixture vars, values, names."""
        from itertools import product

        # product of fixture variable names = concatenation
        fixture_vars_return = fixture_vars + pytest_fixture_vars

        # this is needed because pytest unwraps 1-tuples automatically
        # but subsequent code assumes params is k-tuple, no matter what k is
        if len(fixture_vars) == 1:
            fixture_prod = [(x,) for x in fixture_prod]

        # product of fixture products = Cartesian product plus append tuples
        fixture_prod_return = product(fixture_prod, pytest_fixture_prod)
        fixture_prod_return = [sum(x, ()) for x in fixture_prod_return]

        # product of fixture names = Cartesian product plus concat
        fixture_names_return = product(fixture_names, pytest_fixture_names)
        fixture_names_return = ["-".join(x) for x in fixture_names_return]

        return fixture_vars_return, fixture_prod_return, fixture_names_return


class TestAllObjects(BaseFixtureGenerator, QuickTester):
    """Package level tests for all sktime objects."""

    estimator_type_filter = "object"

    def test_doctest_examples(self, estimator_class):
        """Runs doctests for estimator class."""
        from skbase.utils.doctest_run import run_doctest

        run_doctest(estimator_class, name=f"class {estimator_class.__name__}")

    def test_create_test_instance(self, estimator_class):
        """Check create_test_instance logic and basic constructor functionality.

        create_test_instance and create_test_instances_and_names are the
        key methods used to create test instances in testing.
        If this test does not pass, validity of the other tests cannot be guaranteed.

        Also tests inheritance and super call logic in the constructor.

        Tests that:
        * create_test_instance results in an instance of estimator_class
        * __init__ calls super.__init__
        * _tags_dynamic attribute for tag inspection is present after construction
        """
        estimator = estimator_class.create_test_instance()

        # Check that init does not construct object of other class than itself
        assert isinstance(estimator, estimator_class), (
            "object returned by create_test_instance must be an instance of the class, "
            f"found {type(estimator)}"
        )

        msg = (
            f"{estimator_class.__name__}.__init__ should call "
            f"super({estimator_class.__name__}, self).__init__, "
            "but that does not seem to be the case. Please ensure to call the "
            f"parent class's constructor in {estimator_class.__name__}.__init__"
        )
        assert hasattr(estimator, "_tags_dynamic"), msg

    def test_get_test_params(self, estimator_class):
        """Check that get_test_params returns valid parameter sets."""
        param_list = estimator_class.get_test_params()

        assert isinstance(param_list, (list, dict)), (
            f"{estimator_class.__name__}.get_test_params must "
            "return list of dict or dict, "
            f"found object of type {type(param_list)}"
        )
        if isinstance(param_list, dict):
            param_list = [param_list]
        msg = (
            f"{estimator_class.__name__}.get_test_params must "
            "return list of dict or dict, "
            f"found {param_list}"
        )
        assert all(isinstance(x, dict) for x in param_list), msg

        def _coerce_to_list_of_str(obj):
            if isinstance(obj, str):
                return obj
            elif isinstance(obj, list):
                return obj
            else:
                return []

        reserved_param_names = estimator_class.get_class_tag(
            "reserved_params", tag_value_default=None
        )
        reserved_param_names = _coerce_to_list_of_str(reserved_param_names)

        param_names = estimator_class.get_param_names()

        key_list = [x.keys() for x in param_list]

        notfound_errs = [set(x).difference(param_names) for x in key_list]
        notfound_errs = [x for x in notfound_errs if len(x) > 0]

        assert len(notfound_errs) == 0, (
            f"{estimator_class.__name__}.get_test_params return dict keys "
            f"must be valid parameter names of {estimator_class.__name__}, "
            "i.e., names of arguments of __init__, "
            f"but found some parameters that are not __init__ args: {notfound_errs}"
        )

    def test_get_test_params_coverage(self, estimator_class):
        """Check that get_test_params has good test coverage.

        Checks that:

        * get_test_params returns at least two test parameter sets
        """
        param_list = estimator_class.get_test_params()

        if isinstance(param_list, dict):
            param_list = [param_list]

        def _coerce_to_list_of_str(obj):
            if isinstance(obj, str):
                return obj
            elif isinstance(obj, list):
                return obj
            else:
                return []

        reserved_param_names = estimator_class.get_class_tag(
            "reserved_params", tag_value_default=None
        )
        reserved_param_names = _coerce_to_list_of_str(reserved_param_names)
        reserved_set = set(reserved_param_names)

        param_names = estimator_class.get_param_names()
        unreserved_param_names = set(param_names).difference(reserved_set)

        # commenting out "no reserved params in test params for now"
        # probably cannot ask for that, e.g., index/columns in BaseDistribution

        # key_list = [x.keys() for x in param_list]

        # reserved_errs = [set(x).intersection(reserved_set) for x in key_list]
        # reserved_errs = [x for x in reserved_errs if len(x) > 0]

        # assert len(reserved_errs) == 0, (
        #     "get_test_params return dict keys must be valid parameter names, "
        #     "i.e., names of arguments of __init__ that are not reserved, "
        #     f"but found the following reserved parameters as keys: {reserved_errs}"
        # )

        if len(unreserved_param_names) > 0:
            msg = (
                f"{estimator_class.__name__}.get_test_params should return "
                f"at least two test parameter sets, but only {len(param_list)} found."
            )
            assert len(param_list) > 1, msg

        # this test is too harsh for the current estimator base
        # params_tested = set()
        # for params in param_list:
        #    params_tested = params_tested.union(params.keys())
        # params_not_tested = set(unreserved_param_names).difference(params_tested)
        # assert len(params_not_tested) == 0, (
        #     f"get_test_params should set each parameter of {estimator_class} "
        #     f"to a non-default value at least once, but the following "
        #     f"parameters are not tested: {params_not_tested}"
        # )

    def test_create_test_instances_and_names(self, estimator_class):
        """Check that create_test_instances_and_names works.

        create_test_instance and create_test_instances_and_names are the key methods
        used to create test instances in testing. If this test does not pass, validity
        of the other tests cannot be guaranteed.

        Tests expected function signature of create_test_instances_and_names.
        """
        estimators, names = estimator_class.create_test_instances_and_names()

        assert isinstance(estimators, list), (
            "first return of create_test_instances_and_names must be a list, "
            f"found {type(estimators)}"
        )
        assert isinstance(names, list), (
            "second return of create_test_instances_and_names must be a list, "
            f"found {type(names)}"
        )

        assert np.all([isinstance(est, estimator_class) for est in estimators]), (
            "list elements of first return returned by create_test_instances_and_names "
            "all must be an instance of the class"
        )

        assert np.all([isinstance(name, str) for name in names]), (
            "list elements of second return returned by create_test_instances_and_names"
            " all must be strings"
        )

        assert len(estimators) == len(names), (
            "the two lists returned by create_test_instances_and_names must have "
            "equal length"
        )

    def test_estimator_tags(self, estimator_class):
        """Check conventions on estimator tags."""
        Estimator = estimator_class

        assert hasattr(Estimator, "get_class_tags")
        all_tags = Estimator.get_class_tags()
        assert isinstance(all_tags, dict)
        assert all(isinstance(key, str) for key in all_tags.keys())
        if hasattr(Estimator, "_tags"):
            tags = Estimator._tags
            msg = (
                f"_tags attribute of {estimator_class} must be dict, "
                f"but found {type(tags)}"
            )
            assert isinstance(tags, dict), msg
            assert len(tags) > 0, f"_tags dict of class {estimator_class} is empty"
            invalid_tags = [
                tag for tag in tags.keys() if tag not in VALID_ESTIMATOR_TAGS
            ]
            assert len(invalid_tags) == 0, (
                f"_tags of {estimator_class} contains invalid tags: {invalid_tags}. "
                "For a list of valid tags, see registry.all_tags, or registry._tags. "
            )

        # Avoid ambiguous class attributes
        ambiguous_attrs = ("tags", "tags_")
        for attr in ambiguous_attrs:
            assert not hasattr(Estimator, attr), (
                f"Please avoid using the {attr} attribute to disambiguate it from "
                f"estimator tags."
            )

    def test_inheritance(self, estimator_class):
        """Check that estimator inherits from BaseObject and/or BaseEstimator."""
        assert issubclass(estimator_class, BaseObject), (
            f"object {estimator_class} is not a sub-class of BaseObject."
        )

        if hasattr(estimator_class, "fit"):
            assert issubclass(estimator_class, BaseEstimator), (
                f"estimator: {estimator_class} has fit method, but"
                f"is not a sub-class of BaseEstimator."
            )

        est_scitypes = scitype(
            estimator_class, force_single_scitype=False, coerce_to_list=True
        )

        class_lookup = get_base_class_lookup()

        for est_scitype in est_scitypes:
            if est_scitype in class_lookup:
                expected_parent = class_lookup[est_scitype]
                msg = (
                    f"Estimator: {estimator_class} is tagged as having scitype "
                    f"{est_scitype} via tag object_type, but is not a sub-class of "
                    f"the corresponding base class {expected_parent.__name__}."
                )
                assert issubclass(estimator_class, expected_parent), msg

    def test_has_common_interface(self, estimator_class):
        """Check estimator implements the common interface."""
        estimator = estimator_class

        is_est = issubclass(estimator_class, BaseEstimator)

        # Check class for type of attribute
        if issubclass(estimator_class, BaseEstimator):
            assert isinstance(estimator.is_fitted, property)

        est_scitype = scitype(estimator_class)
        required_methods = _list_required_methods(est_scitype, is_est=is_est)

        for attr in required_methods:
            assert hasattr(estimator, attr), (
                f"Estimator: {estimator.__name__} does not implement attribute: {attr}"
            )

        if hasattr(estimator, "inverse_transform"):
            assert hasattr(estimator, "transform")
        if hasattr(estimator, "predict_proba"):
            assert hasattr(estimator, "predict")

    def test_no_cross_test_side_effects_part1(self, estimator_instance):
        """Test that there are no side effects across tests, through estimator state."""
        estimator_instance.test__attr = 42

    def test_no_cross_test_side_effects_part2(self, estimator_instance):
        """Test that there are no side effects across tests, through estimator state."""
        assert not hasattr(estimator_instance, "test__attr")

    @pytest.mark.parametrize("a", [True, 42])
    def test_no_between_test_case_side_effects(self, estimator_instance, scenario, a):
        """Test that there are no side effects across instances of the same test."""
        assert not hasattr(estimator_instance, "test__attr")
        estimator_instance.test__attr = 42

    def test_get_params(self, estimator_instance):
        """Check that get_params works correctly."""
        estimator = estimator_instance
        params = estimator.get_params()
        assert isinstance(params, dict)

        e = estimator.clone()

        shallow_params = e.get_params(deep=False)
        deep_params = e.get_params(deep=True)

        assert all(item in deep_params.items() for item in shallow_params.items())

    def test_set_params(self, estimator_instance):
        """Check that set_params works correctly."""
        estimator = estimator_instance
        params = estimator.get_params()

        msg = f"set_params of {type(estimator).__name__} does not return self"
        assert estimator.set_params(**params) is estimator, msg

        is_equal, equals_msg = deep_equals(
            estimator.get_params(), params, return_msg=True
        )
        msg = (
            f"get_params result of {type(estimator).__name__} (x) does not match "
            f"what was passed to set_params (y). Reason for discrepancy: {equals_msg}"
        )
        assert is_equal, msg

    def test_set_params_sklearn(self, estimator_class):
        """Check that set_params works correctly, mirrors sklearn check_set_params.

        Instead of the "fuzz values" in sklearn's check_set_params, we use the other
        test parameter settings (which are assumed valid). This guarantees settings
        which play along with the __init__ content.
        """
        estimator = estimator_class.create_test_instance()
        test_params = estimator_class.get_test_params()
        if not isinstance(test_params, list):
            test_params = [test_params]

        reserved_params = estimator_class.get_class_tag(
            "reserved_params", tag_value_default=[]
        )

        for params in test_params:
            # we construct the full parameter set for params
            # params may only have parameters that are deviating from defaults
            # in order to set non-default parameters back to defaults
            params_full = estimator_class.get_param_defaults()
            params_full.update(params)

            msg = f"set_params of {estimator_class.__name__} does not return self"
            est_after_set = estimator.set_params(**params_full)
            assert est_after_set is estimator, msg

            def unreserved(params):
                return {p: v for p, v in params.items() if p not in reserved_params}

            est_params = estimator.get_params(deep=False)
            is_equal, equals_msg = deep_equals(
                unreserved(est_params), unreserved(params_full), return_msg=True
            )
            msg = (
                f"get_params result of {estimator_class.__name__} (x) does not match "
                f"what was passed to set_params (y). "
                f"Reason for discrepancy: {equals_msg}"
            )
            assert is_equal, msg

    def test_clone(self, estimator_instance):
        """Check that clone method does not raise exceptions and results in a clone.

        A clone of an object x is an object that:
        * has same class and parameters as x
        * is not identical with x
        * is unfitted (even if x was fitted)
        """
        est_clone = estimator_instance.clone()
        assert isinstance(est_clone, type(estimator_instance))
        assert est_clone is not estimator_instance
        if hasattr(est_clone, "is_fitted"):
            assert not est_clone.is_fitted

    def test_repr(self, estimator_instance):
        """Check that __repr__ call to instance does not raise exceptions."""
        estimator = estimator_instance
        repr(estimator)

    def test_repr_html(self, estimator_instance):
        """Check that _repr_html_ call to instance does not raise exceptions."""
        estimator_instance._repr_html_()

    def test_constructor(self, estimator_class):
        """Check that the constructor has sklearn compatible signature and behaviour.

        Based on sklearn check_estimator testing of __init__ logic.
        Uses create_test_instance to create an instance.
        Assumes test_create_test_instance has passed and certified create_test_instance.

        Tests that:
        * constructor has no varargs
        * tests that constructor constructs an instance of the class
        * tests that all parameters are set in init to an attribute of the same name
        * tests that parameter values are always copied to the attribute and not changed
        * tests that default parameters are one of the following:
            None, str, int, float, bool, tuple, function, joblib memory, numpy primitive
            (other type parameters should be None, default handling should be by writing
            the default to attribute of a different name, e.g., my_param_ not my_param)
        """
        msg = "constructor __init__ should have no varargs"
        assert getfullargspec(estimator_class.__init__).varkw is None, msg

        estimator = estimator_class.create_test_instance()
        assert isinstance(estimator, estimator_class)

        # Ensure that each parameter is set in init
        init_params = _get_args(type(estimator).__init__)
        invalid_attr = set(init_params) - set(vars(estimator)) - {"self"}
        assert not invalid_attr, (
            "Estimator %s should store all parameters"
            " as an attribute during init. Did not find "
            "attributes `%s`." % (estimator.__class__.__name__, sorted(invalid_attr))
        )

        # Ensure that init does nothing but set parameters
        # No logic/interaction with other parameters
        def param_filter(p):
            """Identify hyper parameters of an estimator."""
            return p.name != "self" and p.kind not in [p.VAR_KEYWORD, p.VAR_POSITIONAL]

        init_params = [
            p
            for p in signature(estimator.__init__).parameters.values()
            if param_filter(p)
        ]

        params = estimator.get_params()

        test_params = estimator_class.get_test_params()
        if isinstance(test_params, list):
            test_params = test_params[0]
        test_params = test_params.keys()

        init_params = [param for param in init_params if param.name not in test_params]

        allowed_param_types = [
            str,
            int,
            float,
            bool,
            tuple,
            type(None),
            np.float64,
            types.FunctionType,
        ]
        if _check_soft_dependencies("joblib", severity="none"):
            from joblib import Memory

            allowed_param_types += [Memory]

        for param in init_params:
            assert param.default != param.empty, (
                "parameter `%s` for %s has no default value and is not "
                "set in `get_test_params`" % (param.name, estimator.__class__.__name__)
            )
            if type(param.default) is type:
                assert param.default in [np.float64, np.int64]
            else:
                assert type(param.default) in allowed_param_types

            reserved_params = estimator_class.get_class_tag("reserved_params", [])
            if param.name not in reserved_params:
                param_value = params[param.name]
                if isinstance(param_value, np.ndarray):
                    np.testing.assert_array_equal(param_value, param.default)
                elif bool(
                    isinstance(param_value, numbers.Real) and np.isnan(param_value)
                ):
                    # Allows to set default parameters to np.nan
                    assert param_value is param.default, param.name
                else:
                    assert param_value == param.default, param.name

    def test_valid_estimator_class_tags(self, estimator_class):
        """Check that Estimator class tags are in VALID_ESTIMATOR_TAGS."""
        for tag in estimator_class.get_class_tags().keys():
            msg = "Found invalid tag: %s" % tag
            assert tag in VALID_ESTIMATOR_TAGS, msg

    def test_valid_estimator_tags(self, estimator_instance):
        """Check that Estimator tags are in VALID_ESTIMATOR_TAGS."""
        for tag in estimator_instance.get_tags().keys():
            msg = "Found invalid tag: %s" % tag
            assert tag in VALID_ESTIMATOR_TAGS, msg


class TestAllEstimators(BaseFixtureGenerator, QuickTester):
    """Package level tests for all sktime estimators, i.e., objects with fit."""

    def test_fit_updates_state(self, estimator_instance, scenario):
        """Check fit/update state change."""
        # Check that fit updates the is-fitted states
        attrs = ["_is_fitted", "is_fitted"]

        estimator = estimator_instance
        estimator_class = type(estimator_instance)

        msg = (
            f"{estimator_class.__name__}.__init__ should call "
            f"super({estimator_class.__name__}, self).__init__, "
            "but that does not seem to be the case. Please ensure to call the "
            f"parent class's constructor in {estimator_class.__name__}.__init__"
        )
        assert hasattr(estimator, "_is_fitted"), msg

        # Check is_fitted attribute is set correctly to False before fit, at init
        for attr in attrs:
            assert not getattr(estimator, attr), (
                f"Estimator: {estimator} does not initiate attribute: {attr} to False"
            )

        fitted_estimator = scenario.run(estimator_instance, method_sequence=["fit"])

        # Check is_fitted attributes are updated correctly to True after calling fit
        for attr in attrs:
            assert getattr(fitted_estimator, attr), (
                f"Estimator: {estimator} does not update attribute: {attr} during fit"
            )

    def test_fit_returns_self(self, estimator_instance, scenario):
        """Check that fit returns self."""
        fit_return = scenario.run(estimator_instance, method_sequence=["fit"])
        assert fit_return is estimator_instance, (
            f"Estimator: {estimator_instance} does not return self when calling fit"
        )

    def test_raises_not_fitted_error(self, estimator_instance, scenario, method_nsc):
        """Check exception raised for non-fit method calls to unfitted estimators.

        Tries to run all methods in NON_STATE_CHANGING_METHODS with valid scenario,
        but before fit has been called on the estimator.

        This should raise a NotFittedError if correctly caught,
        normally by a self.check_is_fitted() call in the method's boilerplate.

        Raises
        ------
        Exception if NotFittedError is not raised by non-state changing method
        """
        # pairwise transformers are exempted from this test, since they have no fitting
        PWTRAFOS = (BasePairwiseTransformer, BasePairwiseTransformerPanel)
        excepted = isinstance(estimator_instance, PWTRAFOS)
        if excepted:
            return None

        # call methods without prior fitting and check that they raise NotFittedError
        with pytest.raises(NotFittedError, match=r"has not been fitted"):
            scenario.run(estimator_instance, method_sequence=[method_nsc])

    def test_fit_idempotent(self, estimator_instance, scenario, method_nsc_arraylike):
        """Check that calling fit twice is equivalent to calling it once."""
        estimator = estimator_instance

        # for now, we have to skip predict_proba, since current output comparison
        #   does not work for tensorflow Distribution
        if (
            isinstance(estimator_instance, BaseForecaster)
            and method_nsc_arraylike == "predict_proba"
        ):
            return None

        # run fit plus method_nsc once, save results
        set_random_state(estimator)
        if method_nsc_arraylike in ["predict_proba", "predict_var"]:
            with ValidProbaErrors() as handler:
                results = scenario.run(
                    estimator,
                    method_sequence=["fit", method_nsc_arraylike],
                    return_all=True,
                    deepcopy_return=True,
                )
            if handler.skipped:
                return None
        else:
            results = scenario.run(
                estimator,
                method_sequence=["fit", method_nsc_arraylike],
                return_all=True,
                deepcopy_return=True,
            )

        estimator = results[0]
        set_random_state(estimator)

        # run fit plus method_nsc a second time
        results_2nd = scenario.run(
            estimator,
            method_sequence=["fit", method_nsc_arraylike],
            return_all=True,
            deepcopy_return=True,
        )

        # check results are equal
        _assert_array_almost_equal(
            results[1],
            results_2nd[1],
            # err_msg=f"Idempotency check failed for method {method}",
        )

    def test_fit_does_not_overwrite_hyper_params(self, estimator_instance, scenario):
        """Check that we do not overwrite hyper-parameters in fit."""
        estimator = estimator_instance
        set_random_state(estimator)

        # Make a physical copy of the original estimator parameters before fitting.
        params = estimator.get_params()
        original_params = deepcopy(params)

        # Fit the model
        fitted_est = scenario.run(estimator_instance, method_sequence=["fit"])

        # Compare the state of the model parameters with the original parameters
        new_params = fitted_est.get_params()
        for param_name, original_value in original_params.items():
            new_value = new_params[param_name]

            # We should never change or mutate the internal state of input
            # parameters by default. To check this we use the joblib.hash function
            # that introspects recursively any subobjects to compute a checksum.
            # The only exception to this rule of immutable constructor parameters
            # is possible RandomState instance but in this check we explicitly
            # fixed the random_state params recursively to be integer seeds.
            msg = (
                "Estimator %s should not change or mutate "
                " the parameter %s from %s to %s during fit."
                % (estimator.__class__.__name__, param_name, original_value, new_value)
            )
            # joblib.hash has problems with pandas objects, so we use deep_equals then
            if isinstance(original_value, (pd.DataFrame, pd.Series)):
                assert deep_equals(new_value, original_value), msg
            elif _check_soft_dependencies("joblib", severity="none"):
                from joblib import hash

                assert hash(new_value) == hash(original_value), msg

    def test_non_state_changing_method_contract(
        self, estimator_instance, scenario, method_nsc
    ):
        """Check that non-state-changing methods behave as per interface contract.

        Check the following contract on non-state-changing methods:
        1. do not change state of the estimator, i.e., any attributes
            (including hyper-parameters and fitted parameters)
        2. expected output type of the method matches actual output type
            - only for abstract BaseEstimator methods, common to all estimator scitypes
            list of BaseEstimator methods tested: get_fitted_params
            scitype specific method outputs are tested in TestAll[estimatortype] class
        """
        estimator = estimator_instance
        set_random_state(estimator)

        # dict_before = copy of dictionary of estimator before predict, post fit
        _ = scenario.run(estimator, method_sequence=["fit"])
        dict_before = estimator.__dict__.copy()

        # skip test if vectorization would be necessary and method predict_proba
        # this is since vectorization is not implemented for predict_proba
        if method_nsc in ["predict_proba", "predict_var"]:
            with ValidProbaErrors() as handler:
                scenario.run(estimator, method_sequence=[method_nsc])
            if handler.skipped:
                return None

        # dict_after = dictionary of estimator after predict and fit
        output = scenario.run(estimator, method_sequence=[method_nsc])
        dict_after = estimator.__dict__

        is_equal, msg = deep_equals(dict_after, dict_before, return_msg=True)
        assert is_equal, (
            f"Estimator: {type(estimator).__name__} changes __dict__ "
            f"during {method_nsc}, "
            f"reason/location of discrepancy (x=after, y=before): {msg}"
        )

        # once there are more methods, this may have to be factored out
        # for now, there is only get_fitted_params and we test here to avoid fit calls
        if method_nsc == "get_fitted_params":
            msg = (
                f"get_fitted_params of {type(estimator)} should return dict, "
                f"but returns object of type {type(output)}"
            )
            assert isinstance(output, dict), msg
            msg = (
                f"get_fitted_params of {type(estimator)} should return dict with "
                f"with str keys, but some keys are not str"
            )
            nonstr = [x for x in output.keys() if not isinstance(x, str)]
            if not len(nonstr) == 0:
                msg = f"found non-str keys in get_fitted_params return: {nonstr}"
                raise AssertionError(msg)

    def test_methods_have_no_side_effects(
        self, estimator_instance, scenario, method_nsc
    ):
        """Check that calling methods has no side effects on args."""
        estimator = estimator_instance

        # skip test for get_fitted_params, as this does not have mutable arguments
        if method_nsc == "get_fitted_params":
            return None

        set_random_state(estimator)

        # Fit the model, get args before and after
        _, args_after = scenario.run(
            estimator, method_sequence=["fit"], return_args=True
        )
        fit_args_after = args_after[0]
        fit_args_before = scenario.args["fit"]

        assert deep_equals(fit_args_before, fit_args_after), (
            f"Estimator: {estimator} has side effects on arguments of fit"
        )

        # skip test if vectorization would be necessary and method predict_proba
        # this is since vectorization is not implemented for predict_proba
        if method_nsc in ["predict_proba", "predict_var"]:
            with ValidProbaErrors() as handler:
                scenario.run(estimator, method_sequence=[method_nsc])
            if handler.skipped:
                return None

        # Fit the model, get args before and after
        _, args_after = scenario.run(
            estimator, method_sequence=[method_nsc], return_args=True
        )
        method_args_after = args_after[0]
        method_args_before = scenario.get_args(method_nsc, estimator)

        assert deep_equals(method_args_after, method_args_before), (
            f"Estimator: {estimator} has side effects on arguments of {method_nsc}"
        )

    def test_persistence_via_pickle(
        self, estimator_instance, scenario, method_nsc_arraylike
    ):
        """Check that we can pickle all estimators."""
        method_nsc = method_nsc_arraylike
        estimator = estimator_instance
        is_forecaster = scitype(estimator) == "forecaster"

        # escape predict_proba for forecasters, skpro distributions cannot be pickled
        if is_forecaster and method_nsc == "predict_proba":
            return None

        # escape predict_var for forecasters if skpro is not available
        skpro_available = _check_soft_dependencies("skpro", severity="none")
        if is_forecaster and method_nsc == "predict_var" and not skpro_available:
            return None

        # escape Deep estimators if soft-dep `h5py` isn't installed
        if isinstance(
            estimator_instance, (BaseDeepClassifier, BaseDeepRegressor)
        ) and not _check_soft_dependencies("h5py", severity="warning"):
            return None

        set_random_state(estimator)
        # Fit the model, get args before and after
        scenario.run(estimator, method_sequence=["fit"], return_args=True)

        # Generate results before pickling
        vanilla_result = scenario.run(estimator, method_sequence=[method_nsc])

        # Serialize and deserialize
        serialized_estimator = estimator.save()
        deserialized_estimator = load(serialized_estimator)

        deserialized_result = scenario.run(
            deserialized_estimator, method_sequence=[method_nsc]
        )

        msg = (
            f"Results of {method_nsc} differ between when pickling and not pickling, "
            f"estimator {type(estimator_instance).__name__}"
        )
        _assert_array_almost_equal(
            vanilla_result,
            deserialized_result,
            decimal=6,
            err_msg=msg,
        )

    def test_save_estimators_to_file(
        self, estimator_instance, scenario, method_nsc_arraylike
    ):
        """Check if saved estimators onto disk can be loaded correctly."""
        method_nsc = method_nsc_arraylike
        estimator = estimator_instance
        is_forecaster = scitype(estimator) == "forecaster"

        # escape predict_proba for forecasters, skpro distributions cannot be pickled
        if is_forecaster and method_nsc == "predict_proba":
            return None

        # escape predict_var for forecasters if skpro is not available
        skpro_available = _check_soft_dependencies("skpro", severity="none")
        if is_forecaster and method_nsc == "predict_var" and not skpro_available:
            return None

        set_random_state(estimator)
        # Fit the model, get args before and after
        scenario.run(estimator, method_sequence=["fit"], return_args=True)

        # Generate results before saving
        vanilla_result = scenario.run(estimator, method_sequence=[method_nsc])

        with TemporaryDirectory() as tmp_dir:
            save_loc = os.path.join(tmp_dir, "estimator")
            estimator.save(save_loc)

            loaded_estimator = load(save_loc)
            loaded_result = scenario.run(loaded_estimator, method_sequence=[method_nsc])

            msg = (
                f"Results of {method_nsc} differ between saved and loaded "
                f"estimator {type(estimator).__name__}"
            )

            _assert_array_almost_equal(
                vanilla_result,
                loaded_result,
                decimal=6,
                err_msg=msg,
            )

    def test_multiprocessing_idempotent(
        self, estimator_instance, scenario, method_nsc_arraylike
    ):
        """Test that single and multi-process run results are identical.

        Check that running an estimator on a single process is no different to running
        it on multiple processes. We also check that we can set n_jobs=-1 to make use of
        all CPUs. The test is not really necessary though, as we rely on joblib for
        parallelization and can trust that it works as expected.
        """
        method_nsc = method_nsc_arraylike
        params = estimator_instance.get_params()

        # test runs only if n_jobs is a parameter of the estimator
        if "n_jobs" not in params:
            return None

        # skip test for predict_proba
        # this produces a BaseDistribution object, for which no ready
        # equality check is implemented
        is_forecaster = scitype(estimator_instance) == "forecaster"
        if is_forecaster and method_nsc == "predict_proba":
            return None
        # escape predict_proba etc for forecasters if skpro is not available
        skpro_available = _check_soft_dependencies("skpro", severity="none")
        if is_forecaster and method_nsc == "predict_var" and not skpro_available:
            return None

        # run on a single process
        # -----------------------
        estimator = deepcopy(estimator_instance)
        estimator.set_params(n_jobs=1)
        set_random_state(estimator)
        result_single_process = scenario.run(
            estimator, method_sequence=["fit", method_nsc]
        )

        # run on multiple processes
        # -------------------------
        estimator = deepcopy(estimator_instance)
        estimator.set_params(n_jobs=-1)
        set_random_state(estimator)
        result_multiple_process = scenario.run(
            estimator, method_sequence=["fit", method_nsc]
        )

        _assert_array_equal(
            result_single_process,
            result_multiple_process,
            err_msg="Results are not equal for n_jobs=1 and n_jobs=-1",
        )

    def test_dl_constructor_initializes_deeply(self, estimator_class):
        """Test DL estimators that they pass custom parameters to underlying Network."""
        estimator = estimator_class

        if not issubclass(estimator, (BaseDeepClassifier, BaseDeepRegressor)):
            return None

        if not hasattr(estimator, "get_test_params"):
            return None

        params = estimator.get_test_params()

        if isinstance(params, list):
            params = params[0]
        if isinstance(params, dict):
            pass
        else:
            raise TypeError(
                f"`get_test_params()` of estimator: {estimator} returns "
                f"an expected type: {type(params)}, acceptable formats: [list, dict]"
            )

        estimator = estimator(**params)

        for key, value in params.items():
            assert vars(estimator)[key] == value
            # some keys are only relevant to the final model (eg: n_epochs)
            # skip them for the underlying network
            if vars(estimator._network).get(key) is not None:
                assert vars(estimator._network)[key] == value
