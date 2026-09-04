FROM docker.io/library/python:3.13-alpine

# pyyaml + jinja2 are the only deps beyond stdlib (core.py needs both).
RUN pip install --no-cache-dir pyyaml==6.0.2 jinja2==3.1.6

# The code ships via the stack-config mount (config_files), not the image:
# restart picks up changes without a rebuild.
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "/stack-config/orchestrator-server.py"]
