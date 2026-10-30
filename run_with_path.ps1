# Screen Monitor PowerShell Fix
# Adds current directory to PYTHONPATH and runs the app

$env:PYTHONPATH = Get-Location
& 'C:\Program Files\Python312\python.exe' C:\Users\User\Documents\GitHub\screengrab\src\main.py