"""Version del formato de estado.

Se persiste en cada fila de trajectory_steps. Si cambia la forma que produce
serializer.py, ESTE numero sube. El protocolo crudo persistido permite
re-derivar el historico a la version nueva.
"""

STATE_SCHEMA_VERSION = 1
