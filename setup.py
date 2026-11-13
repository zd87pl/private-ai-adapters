"""
Setup script for WDVA.

WDVA: Weight-Delta Vault Adapters (early-stage; see README.md, "Status")

Copyright 2025 Enclave
Licensed under the Apache License, Version 2.0
"""

from pathlib import Path
from setuptools import setup, find_packages

# Read README for long description
readme_path = Path(__file__).parent / "README.md"
long_description = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

# Read version from package
version = "0.1.0.dev0"

setup(
    name="wdva",
    version=version,
    description="Encrypted model adapters and local MLX inference (early-stage)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    
    # Author info
    author="Enclave Team",
    author_email="team@enclave.ai",
    
    # URLs
    url="https://github.com/zd87pl/private-ai-adapters",
    project_urls={
        "Documentation": "https://github.com/zd87pl/private-ai-adapters#readme",
        "Source": "https://github.com/zd87pl/private-ai-adapters",
        "Bug Tracker": "https://github.com/zd87pl/private-ai-adapters/issues",
    },
    
    # Package discovery
    packages=find_packages(exclude=["tests", "tests.*", "examples"]),
    
    # Python version requirement
    python_requires=">=3.10",
    
    # Core dependencies (always required)
    install_requires=[
        "pycryptodome>=3.19.0",
        "cryptography>=41.0.0",
        "safetensors>=0.4.0",
        "numpy>=1.24.0",
    ],
    
    # Optional dependencies
    extras_require={
        # Local inference with MLX (Apple Silicon, Python 3.11+)
        "mlx": [
            "mlx-lm>=0.32.0",
        ],
        # Adapter compression
        "compression": [
            "zstandard>=0.22.0",
        ],
        # Development dependencies
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "pynacl>=1.5.0",
            "zstandard>=0.22.0",
            "mypy>=1.0.0",
            "ruff>=0.1.0",
        ],
    },
    
    # Classifiers
    classifiers=[
        "Development Status :: 2 - Pre-Alpha",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: MacOS :: MacOS X",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Security :: Cryptography",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Typing :: Typed",
    ],
    
    # Keywords for PyPI
    keywords=[
        "wdva",
        "privacy",
        "ai",
        "machine-learning",
        "encryption",
        "lora",
        "adapter",
        "llm",
        "private-ai",
        "mlx",
    ],
    
    # Include package data
    include_package_data=True,
    zip_safe=False,
    
    # Type hints
    package_data={
        "wdva": ["py.typed"],
    },
)
