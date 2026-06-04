"""RAGAS offline evaluation harness for the TocDoc QnA service (P4-2).

This package scores QnA answer quality offline using RAGAS. It imports the QnA
service pipeline read-only and never modifies ``services/`` or ``clients/``.
The ragas/datasets dependencies live in ``eval/requirements.txt`` so they stay
out of the QnA runtime image.
"""
