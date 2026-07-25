"""Host-agnostic session state: the cabmap browser and scene-placement models.

Both modules keep their state in module-level globals, matching how the hosts
already use them: one host application is one process is one session.
"""
