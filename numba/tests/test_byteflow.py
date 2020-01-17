"""
Test byteflow.py specific issues
"""
from numba.tests.support import TestCase
from numba.compiler import run_frontend


class TestByteFlowIssues(TestCase):
    def test_issue_5087(self):
        # This is an odd issue. The number of exact number of print below is
        # necessary to trigger it. Too many or few will alter the behavior.
        # Also note that the function below will not be executed. The problem
        # occurs at compilation. The definition below is invalid for execution.
        # The problem occurs in the bytecode analysis.
        def udt():
            print
            print
            print

            for i in range:
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print
                print

                for j in range:
                    print
                    print
                    print
                    print
                    print
                    print
                    print
                    for k in range:
                        for l in range:
                            print

                    print
                    print
                    print
                    print
                    print
                    print
                    print
                    print
                    print
                    if print:
                        for n in range:
                            print
                    else:
                        print

        run_frontend(udt)
