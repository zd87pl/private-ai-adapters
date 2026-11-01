"""
Setup script for WDVA boilerplate.

WDVA: Weight-Delta Vault Adapters
Secure & Private AI for Everyone

Copyright 2025 Enclave
Licensed under the Apache License, Version 2.0
"""

from pathlib import Path
from setuptools import setup, find_packages

# Read README for long description
readme_path = Path(__file__).parent / "README.md"
long_description = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

# Read version from package
version = "1.0.0"

setup(
    name="wdva",
    version=version,
    description="Weight-Delta Vault Adapters - Secure & Private AI for Everyone",
    long_description=long_description,
    long_description_content_type="text/markdown",
    
    # Author info
    author="Enclave Team",
    author_email="team@enclave.ai",
    
    # URLs
    url="https://github.com/enclave-ai/wdva-boilerplate",
    project_urls={
        "Documentation": "https://github.com/enclave-ai/wdva-boilerplate#readme",
        "Source": "https://github.com/enclave-ai/wdva-boilerplate",
        "Bug Tracker": "https://github.com/enclave-ai/wdva-boilerplate/issues",
    },
    
    # Package discovery
    packages=find_packages(exclude=["tests", "tests.*", "examples"]),
    
    # Python version requirement
    python_requires=">=3.9",
    
    # Core dependencies (always required)
    install_requires=[
        "pycryptodome>=3.19.0",
        "cryptography>=41.0.0",
        "safetensors>=0.4.0",
        "numpy>=1.24.0",
    ],
    
    # Optional dependencies
    extras_require={
        # MLX backend (Apple Silicon)
        "mlx": [
            "mlx>=0.18.0",
            "mlx-lm>=0.1.0",
        ],
        # PyTorch backend (CPU/CUDA)
        "torch": [
            "torch>=2.0.0",
            "transformers>=4.35.0",
        ],
        # arXiv example dependencies
        "arxiv": [
            "requests>=2.31.0",
            "PyPDF2>=3.0.0",
        ],
        # LangChain integration (core)
        "langchain": [
            "langchain-core>=0.1.0",
            "langchain>=0.1.0",
        ],
        # LangChain integration (full with RAG support)
        "langchain-full": [
            "langchain-core>=0.1.0",
            "langchain>=0.1.0",
            "langchain-community>=0.1.0",
            "sentence-transformers>=2.2.0",
            "faiss-cpu>=1.7.0",
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
        # All optional dependencies
        "all": [
            "mlx>=0.18.0",
            "mlx-lm>=0.1.0",
            "torch>=2.0.0",
            "transformers>=4.35.0",
            "requests>=2.31.0",
            "PyPDF2>=3.0.0",
            "zstandard>=0.22.0",
            "hf-transfer>=0.1.0",
            "langchain-core>=0.1.0",
            "langchain>=0.1.0",
            "langchain-community>=0.1.0",
            "sentence-transformers>=2.2.0",
        ],
    },
    
    # Classifiers
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
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
        "dora",
        "lora",
        "adapter",
        "llm",
        "private-ai",
        "secure-ai",
    ],
    
    # Include package data
    include_package_data=True,
    zip_safe=False,
    
    # Type hints
    package_data={
        "wdva": ["py.typed"],
    },
)
