"""Utility functions for the Options Trading Helper."""

import os
import logging
from datetime import datetime, date
from typing import Optional
import yaml
from dotenv import load_dotenv


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Set up logging configuration.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional path to log file
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger("options-bot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(console_format)
        logger.addHandler(file_handler)
    
    return logger


def load_config(config_path: str) -> dict:
    """Load configuration from YAML file.
    
    Args:
        config_path: Path to the YAML configuration file
        
    Returns:
        Configuration dictionary
        
    Raises:
        FileNotFoundError: If config file doesn't exist
        yaml.YAMLError: If config file is invalid
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    return config


def load_environment() -> dict:
    """Load environment variables from .env file.
    
    Returns:
        Dictionary with environment configuration
    """
    load_dotenv()
    
    return {
        "etrade_consumer_key": os.getenv("ETRADE_CONSUMER_KEY"),
        "etrade_consumer_secret": os.getenv("ETRADE_CONSUMER_SECRET"),
        "etrade_mode": os.getenv("ETRADE_MODE", "sandbox"),
        # Wetrade browser-based auth credentials
        "etrade_username": os.getenv("ETRADE_USERNAME"),
        "etrade_password": os.getenv("ETRADE_PASSWORD"),
        "etrade_totp_secret": os.getenv("ETRADE_TOTP_SECRET"),
        # Email settings
        "gmail_address": os.getenv("GMAIL_ADDRESS"),
        "gmail_app_password": os.getenv("GMAIL_APP_PASSWORD"),
        "alert_recipient": os.getenv("ALERT_RECIPIENT"),
        "config_path": os.getenv("CONFIG_PATH", "config/config.yaml"),
        # LLM Provider for ticker context
        "llm_provider": os.getenv("LLM_PROVIDER", "gemini"),
        "gemini_cmd": os.getenv("GEMINI_CMD"),
        "claude_cmd": os.getenv("CLAUDE_CMD"),
    }


def find_target_expiration(
    expiration_dates: list[date],
    target_days: int = 45,
    min_days: int = 30,
    max_days: int = 60
) -> Optional[date]:
    """Find the best expiration date closest to target days out.
    
    Args:
        expiration_dates: List of available expiration dates
        target_days: Ideal days to expiration
        min_days: Minimum acceptable days
        max_days: Maximum acceptable days
        
    Returns:
        Best matching expiration date, or None if no suitable date found
    """
    today = date.today()
    
    # Filter to dates within range
    valid_dates = []
    for exp_date in expiration_dates:
        if isinstance(exp_date, datetime):
            exp_date = exp_date.date()
        days_out = (exp_date - today).days
        if min_days <= days_out <= max_days:
            valid_dates.append((exp_date, days_out))
    
    if not valid_dates:
        return None
    
    # Find closest to target
    best_date = min(valid_dates, key=lambda x: abs(x[1] - target_days))
    return best_date[0]


def calculate_annualized_yield(
    premium: float,
    strike_price: float,
    days_to_expiry: int
) -> float:
    """Calculate annualized yield for an options premium.
    
    Args:
        premium: Option premium received
        strike_price: Strike price of the option
        days_to_expiry: Days until expiration
        
    Returns:
        Annualized yield as a decimal (e.g., 0.12 = 12%)
    """
    if strike_price <= 0 or days_to_expiry <= 0:
        return 0.0
    
    # Yield = (premium / strike_price) * (365 / days_to_expiry)
    periodic_yield = premium / strike_price
    annualized = periodic_yield * (365 / days_to_expiry)
    return annualized


def format_currency(value: float) -> str:
    """Format a number as currency."""
    return f"${value:,.2f}"


def format_percentage(value: float) -> str:
    """Format a decimal as percentage."""
    return f"{value * 100:.2f}%"
