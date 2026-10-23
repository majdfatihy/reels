@echo off
title GitHub Auto Uploader - Reels Project
color 0A

echo ===================================================
echo       Preparing and Uploading Project to GitHub
echo ===================================================
echo.

:: 1. Check if Git is installed
git --version >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Git is not installed on your computer!
    echo Please download and install it from: https://git-scm.com/downloads
    pause
    exit /b
)

:: 2. Create .gitignore automatically to skip heavy folders
if not exist .gitignore (
    echo Creating .gitignore file to exclude heavy files...
    (
        echo venv/
        echo __pycache__/
        echo jobs/
        echo *.mp4
        echo *.mkv
        echo *.webm
        echo .env
    ) > .gitignore
)

:: 3. Initialize Git repository if not already initialized
if not exist .git (
    echo Initializing local Git repository...
    git init
    git branch -M main
    git remote add origin https://github.com/majdfatihy/reels.git
) else (
    git remote set-url origin https://github.com/majdfatihy/reels.git 2>nul || git remote add origin https://github.com/majdfatihy/reels.git
)

:: 4. Stage all files and folders (including static/index.html)
echo.
echo Adding all files and folders...
git add .

:: 5. Commit changes
set /p commit_msg="Enter commit message (or press Enter for default): "
if "%commit_msg%"=="" set commit_msg=Update project files and folders
git commit -m "%commit_msg%"

:: 6. Push to GitHub
echo.
echo Uploading to GitHub...
git pull origin main --rebase 2>nul
git push -u origin main --force

echo.
echo ===================================================
echo [SUCCESS] Project uploaded to GitHub successfully!
echo ===================================================
pause