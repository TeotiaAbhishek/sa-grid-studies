FROM python:3.12-slim

# Run as a non-root user.
RUN useradd --create-home sagrid
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER sagrid
WORKDIR /home/sagrid
VOLUME ["/home/sagrid/data"]
ENTRYPOINT ["sagrid"]
CMD ["--help"]
