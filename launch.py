#!/usr/bin/env python
"""Screen Monitor Launcher - Handles path setup automatically."""

import os
import sys
from pathlib import Path

# Add the project root to sys.path
project_root = str(Path(__file__).resolve().parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Now import and run the main application
from src.main import main

if __name__ == "__main__":
    main()
