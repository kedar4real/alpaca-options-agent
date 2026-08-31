def main() -> None:
    """Console-script entry point (``trading-agent``): run the autonomous loop."""
    from .main import Config, run_forever

    run_forever(Config.from_env())
