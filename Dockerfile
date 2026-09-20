# One image, several commands. The API, the MCP server and the CLI share a package,
# a config file and a travel snapshot; building three images to run three entry points
# would mean three things to keep in step for no isolation that ECS does not already
# give us at the task level.

# --- the dispatch board -------------------------------------------------------
# Built here rather than committed, so a stale bundle cannot ship.
FROM node:22-slim AS board
WORKDIR /board
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- python dependencies ------------------------------------------------------
# Separate stage so the compiler and the wheel cache stay out of the runtime image.
FROM python:3.12-slim AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# Dependency metadata alone, so editing source does not reinstall OR-Tools.
COPY pyproject.toml ./
RUN mkdir -p src/glass_guru && touch src/glass_guru/__init__.py && pip install .

# --- runtime ------------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # No OSRM and no Google in a task that has neither. The committed leg snapshot
    # covers the fixture geography; a deployment serving real addresses sets this to
    # `warm` and points GLASS_GURU_OSRM_URL at the routing service.
    GLASS_GURU_TRAVEL=frozen \
    # Bind every interface. The default is loopback so that running the API on a
    # laptop does not expose the board to the local network; in a container loopback
    # means nothing outside can reach it, load balancer included.
    GLASS_GURU_API_HOST=0.0.0.0

RUN useradd --create-home --uid 10001 glass
WORKDIR /app

COPY --from=deps /opt/venv /opt/venv
COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
COPY evals/ ./evals/
COPY --from=board /board/dist ./web/dist

# --no-deps: everything is already in the venv, and resolving again at image build
# time is how a lockfile-free build silently picks up a different version.
RUN pip install --no-deps --no-cache-dir -e . && chown -R glass:glass /app

USER glass
EXPOSE 8000

# Fails the ECS health check on a container that started but cannot plan, which is
# the failure mode worth catching: the process is up and the answer is wrong.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["glass-guru-api"]
