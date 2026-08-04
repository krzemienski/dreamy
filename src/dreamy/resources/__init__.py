"""Packaged data files.

Lives inside the package rather than top-level config/, because the wheel
ships only `src/dreamy`. A schema under config/ is absent after install, and
a validator whose schema is missing in production enforces nothing exactly
where enforcement matters most.
"""
