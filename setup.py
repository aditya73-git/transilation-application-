"""Setup file for the offline translator project"""
from setuptools import setup, find_packages
from pathlib import Path

# Read requirements
requirements_path = Path(__file__).parent / "requirements.txt"
with open(requirements_path) as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="offline-translator",
    version="0.1.0",
    description="Terminal-first offline translator for Raspberry Pi wearable device",
    author="Aditya",
    author_email="",
    packages=find_packages(),
    install_requires=requirements,
    python_requires=">=3.9",
    entry_points={
        "console_scripts": [
            "translator=src.main:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
