"""The Lab's backtesting engine (Group 66): replay, reality penalties, metrics, Monte Carlo, sweeps.

``runner.run_backtest`` is the entry point; ``replay_engine`` turns the market history into the
Hive's signal stream; ``simulator`` trades it; ``time_lock`` keeps every read point-in-time.
"""
