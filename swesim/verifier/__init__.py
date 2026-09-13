"""Offline grader that runs inside the separate Harbor verifier container.

Standard library only. The compile stage copies ``grader.py``, ``policy.py``,
``parsers.py`` and ``run_pytest.py`` into a task's ``tests/`` directory, where
``grader.py`` is executed as a script (``swesim-python -I /tests/grader.py
--config /tests/grader_config.json``) and imports its siblings from ``/tests``.
The same files import as ``swesim.verifier.*`` for unit tests.
"""
