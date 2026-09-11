# syntax=docker/dockerfile:1.7

# ==============================================================================

# BetDoc — production image

#

# Two stages. The builder carries a full C/Fortran toolchain because PyTensor

# compiles C extensions and NumPy/SciPy link against BLAS/LAPACK. The runner

# carries none of it: no compiler, no headers, no package manager state. The

# only artifact that crosses the boundary is the resolved virtualenv.

# ==============================================================================



# ------------------------------------------------------------------------------

# Stage 1: builder

# ------------------------------------------------------------------------------

FROM python:3.12-slim-bookworm AS builder



# gfortran + libblas/liblapack are mandatory, not optional: without them pip

# falls back to building SciPy without an optimised BLAS, and MCMC throughput

# drops by an order of magnitude.

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \

    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \

    apt-get update && apt-get install --no-install-recommends -y \

        build-essential \

        python3-dev \

        libblas-dev \

        liblapack-dev \

        gfortran \

        git \

    && rm -rf /var/lib/apt/lists/*



# Astral's uv resolves and installs an order of magnitude faster than pip and

# produces a deterministic lockfile.

RUN pip install --no-cache-dir uv==0.5.11



ENV UV_PROJECT_ENVIRONMENT=/opt/venv \

    UV_LINK_MODE=copy \

    UV_COMPILE_BYTECODE=1 \

    UV_PYTHON_DOWNLOADS=never



WORKDIR /build



# Dependency manifests only. Source is copied after the sync so that a code

# change does not invalidate the (expensive) dependency layer.

# uv.lock is globbed so the build works whether or not it is committed.

COPY pyproject.toml uv.lock* ./



# NOTE ON REPRODUCIBILITY: `uv lock` is a no-op when a valid, current uv.lock

# is present, and generates one when it is absent. Generating a lockfile at

# build time is *not* reproducible — two builds a week apart can resolve

# different transitive versions. Commit uv.lock to the repository and this

# layer becomes deterministic; `--frozen` below then guarantees the sync

# matches the lock exactly and fails loudly if it cannot.

RUN --mount=type=cache,target=/root/.cache/uv \

    uv lock && \

    uv sync --frozen --no-dev --no-install-project



# Now install the project itself into the same environment.

COPY src/ ./src/

COPY README.md* ./

RUN --mount=type=cache,target=/root/.cache/uv \

    uv sync --frozen --no-dev



# Strip the venv of anything only needed to build. Saves ~40MB and removes

# tooling that would otherwise be reachable in the runtime image.

RUN find /opt/venv -type d -name '__pycache__' -prune -exec rm -rf {} + && \

    find /opt/venv -type d -name 'tests' -prune -exec rm -rf {} + && \

    rm -rf /opt/venv/lib/python3.12/site-packages/pip \

           /opt/venv/lib/python3.12/site-packages/setuptools \

           /opt/venv/lib/python3.12/site-packages/wheel



# ------------------------------------------------------------------------------

# Stage 2: runner

# ------------------------------------------------------------------------------

FROM python:3.12-slim-bookworm AS runner



LABEL org.opencontainers.image.title="betdoc" \

      org.opencontainers.image.description="Bayesian sports trading platform" \

      org.opencontainers.image.vendor="BetDoc" \

      org.opencontainers.image.licenses="Proprietary"



# Runtime-only dependencies. libgomp1 and the shared BLAS/LAPACK runtimes are

# required because the compiled extensions link against them; the *-dev headers

# and compilers are deliberately absent.

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \

    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \

    apt-get update && apt-get install --no-install-recommends -y \

        dumb-init \

        libgomp1 \

        libblas3 \

        liblapack3 \

        libgfortran5 \

        curl \

    && rm -rf /var/lib/apt/lists/*



# Non-root system account. A high, explicitly pinned UID avoids collision with

# host users when volumes are bind-mounted, and keeps the mapping stable across

# rebuilds so file ownership on persisted volumes does not drift.

RUN groupadd --system --gid 10001 betdoc && \

    useradd --system --uid 10001 --gid 10001 \

            --home-dir /home/betdoc-user --create-home \

            --shell /usr/sbin/nologin betdoc-user



COPY --from=builder --chown=root:root /opt/venv /opt/venv



ENV PATH="/opt/venv/bin:${PATH}" \

    PYTHONUNBUFFERED=1 \

    PYTHONDONTWRITEBYTECODE=1 \

    PYTHONFAULTHANDLER=1 \

    PYTHONHASHSEED=random \

    # ---------------------------------------------------------------------

    # Thread pinning. This is not a micro-optimisation; it prevents a hard

    # failure mode. PyMC runs N chains as N processes. Each chain's NumPy

    # calls into OpenBLAS, which by default spawns one thread per core. With

    # 4 chains on a 16-core box that is 64 threads fighting over 2 allocated

    # CPUs: the scheduler thrashes, sampling slows by 10-50x, and the

    # container trips its memory limit building thread stacks. One BLAS

    # thread per chain process is strictly faster here.

    # ---------------------------------------------------------------------

    OMP_NUM_THREADS=1 \

    OPENBLAS_NUM_THREADS=1 \

    MKL_NUM_THREADS=1 \

    NUMEXPR_NUM_THREADS=1 \

    VECLIB_MAXIMUM_THREADS=1 \

    # PyTensor compiles and caches C extensions at runtime. As a non-root user

    # with a read-only root filesystem it cannot write to the default

    # ~/.pytensor, and the first pm.sample() dies with a CompileError. Point

    # the compiledir at a tmpfs-backed path that is writable by UID 10001.

    PYTENSOR_FLAGS="base_compiledir=/tmp/pytensor,cxx=" \

    MPLCONFIGDIR=/tmp/matplotlib \

    BETDOC_ENV=production



WORKDIR /app

COPY --from=builder --chown=betdoc-user:betdoc /build/src /app/src



# Writable scratch paths, pre-created with correct ownership so the container

# works with a read_only root filesystem plus tmpfs mounts.

RUN mkdir -p /tmp/pytensor /tmp/matplotlib /app/var/artifacts /app/var/dlq && \

    chown -R betdoc-user:betdoc /tmp/pytensor /tmp/matplotlib /app/var



USER 10001:10001



EXPOSE 8000



# dumb-init as PID 1. Without it, uvicorn/aiokafka inherit PID 1 and Linux

# silently discards default signal dispositions for PID 1 — SIGTERM is then

# ignored, `docker stop` blocks for the full 10s grace period, and the worker

# is SIGKILLed mid-transaction with an uncommitted Kafka offset. dumb-init also

# reaps the zombie children that PyMC's multiprocess sampler leaves behind.

ENTRYPOINT ["dumb-init", "--"]



HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \

    CMD curl --fail --silent --show-error http://127.0.0.1:8000/health || exit 1



CMD ["uvicorn", "betdoc.infrastructure.api.app:app", \

     "--host", "0.0.0.0", "--port", "8000", \

     "--workers", "1", "--loop", "uvloop", "--http", "httptools", \

     "--no-access-log", "--timeout-keep-alive", "65"]
