import os
import sys

# Make the vendored `herdr_client` package importable when these tests are run
# from any working directory: scripts/ (the package's parent) goes on sys.path.
# tests/ -> herdr_client/ -> scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
