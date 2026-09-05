# NOTE: base image tag tracks python:3.14-slim-bookworm; production promotion must pin by immutable digest (see README).
ARG PYTHON_IMAGE=docker.io/library/python:3.14-slim-bookworm
FROM ${PYTHON_IMAGE} AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY meco_news ./meco_news
RUN python -m pip install --no-cache-dir --no-deps setuptools==75.8.0 wheel==0.45.1 \
    && python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /build/wheels .

FROM ${PYTHON_IMAGE} AS runtime

WORKDIR /app
COPY --from=builder /build/wheels/meco_news-*.whl /tmp/
RUN python -m pip install --no-cache-dir --no-index --no-deps /tmp/meco_news-*.whl \
    && rm -f /tmp/meco_news-*.whl \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin meco \
    && mkdir -p /app/data /app/config \
    && chown -R meco:meco /app/data \
    && chmod 0555 /app /usr/local/lib/python3.14/site-packages/meco_news \
    && chmod -R a=rX /usr/local/lib/python3.14/site-packages/meco_news
COPY config ./config
RUN chown -R root:root /app/config && chmod -R a=rX /app/config

USER meco
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "meco_news"]
CMD ["--daemon"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 CMD ["python", "-m", "meco_news", "--healthcheck", "--json"]
