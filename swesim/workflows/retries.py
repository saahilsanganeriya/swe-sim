"""Retry classes: every `execute_activity` names one of these (interfaces §4, brief §2.8 item 2).

Temporal's rule, applied here: retry network errors, timeouts, rate limits and transient unavailability; never retry
invalid input or a business rejection. Activities raise `non_retryable(...)` for gate rejections, schema failures and
"candidate not kept" (and `swesim.workflows.activities` converts every `ValueError` into one); transient provider,
Modal and GitHub failures propagate as ordinary exceptions and are retried by the class the workflow picked.

- LLM   : judge, draft, approval, stakeholder-bound work; provider 429/5xx and timeouts.
- BUILD : Modal / Harbor infrastructure (probe, compile, validate); slower backoff, fewer attempts.
- READ  : polls and index writes; cheap to repeat.
- ONCE  : launch. The launcher is idempotent by design, but a retry must go through `decide_launch`, never a blind relaunch.
"""
from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

LLM = RetryPolicy(initial_interval=timedelta(seconds=10), backoff_coefficient=2.0, maximum_interval=timedelta(minutes=2), maximum_attempts=4)
BUILD = RetryPolicy(initial_interval=timedelta(seconds=30), backoff_coefficient=2.0, maximum_interval=timedelta(minutes=5), maximum_attempts=3)
READ = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0, maximum_interval=timedelta(minutes=1), maximum_attempts=5)
ONCE = RetryPolicy(maximum_attempts=1)
# Bookkeeping hooks (index writes, cost accounting): READ, but an activity type no worker registers is not worth
# five attempts; "NotFoundError" is the type the SDK's worker raises for it.
HOOK = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0, maximum_interval=timedelta(minutes=1), maximum_attempts=5,
                   non_retryable_error_types=["NotFoundError"])


def non_retryable(msg: str, *, kind: str) -> ApplicationError:
    """A refused decision or invalid input: the activity fails at once and the workflow reads the reason."""
    return ApplicationError(msg, type=kind, non_retryable=True)


def transient(msg: str, *, kind: str) -> ApplicationError:
    """An explicit retryable failure with a type (provider outage, infrastructure); the retry class decides how often."""
    return ApplicationError(msg, type=kind, non_retryable=False)
