# A deployable image for the REST surface.
#
# Not used by `make up` or by the test suite -- those run the app from the
# working tree against the compose database. This exists so a container host
# (Render, Fly, Koyeb) can build the API without a shell script.

FROM python:3.11-slim

# psycopg[binary] ships its own libpq, so no build toolchain is needed here.
WORKDIR /app

# Dependency metadata and sources are copied before install so the layer is
# reused whenever only rulepacks or corpora change.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY rulepacks/ ./rulepacks/
COPY corpora/ ./corpora/

# Hosts inject the port they want bound; 8000 is only the local default.
ENV PORT=8000
EXPOSE 8000

# exec form wrapping an explicit `exec`: ${PORT} still expands at run time, but
# uvicorn replaces the shell as PID 1 instead of running as its child. Without
# the exec, SIGTERM on redeploy goes to sh and never reaches uvicorn, so the
# host waits out its grace period on every single deploy.
CMD ["sh", "-c", "exec uvicorn doctask.asgi:app --host 0.0.0.0 --port ${PORT}"]
