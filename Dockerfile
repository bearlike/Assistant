# Dockerfile to build meeseeks-chat with core dependencies.

FROM python:3.11-buster

# Set the title, GitHub repo URL, version, and author
ARG TITLE="Meeseeks Chat: Personal Assistant" \
    VERSION="1.0.0" \
    AUTHOR="Krishnakanth Alagiri"

LABEL org.opencontainers.image.source="https://github.com/bearlike/Personal-Assistant" \
    org.opencontainers.image.version=$VERSION \
    org.opencontainers.image.vendor=$AUTHOR \
    org.opencontainers.image.licenses="mail@kanth.tech" \
    org.opencontainers.image.licenses="MIT"

LABEL maintainer=$AUTHOR \
    title=$TITLE \
    url=$GITHUB_REPO_URL \
    version=$VERSION

# Virtualens are redundant for Dockerfile.
ENV POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache \
    POETRY_VIRTUALENVS_CREATE=false

# Update and install necessary software
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    software-properties-common \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory to /app (assuming project root)
WORKDIR /app

# Copy the repo contents to the Docker image
COPY . /app

# Install Poetry
RUN pip install 'poetry>=1.8,<1.9'

# Install the core dependencies
RUN poetry install

# Install the meeseeks-chat dependencies
WORKDIR /app/meeseeks-chat
RUN poetry install

# Set default environment variablesfor Meeseeks
ENV CACHE_DIR='/tmp/meeseeks_cache' \
    DEFAULT_MODEL='gpt-3.5-turbo' \
    LOG_LEVEL=DEBUG \
    ENVMODE=dev \
    VERSION=${VERSION} \
    COLOREDLOGS_FIELD_STYLES='asctime=color=240;name=45,inverse' \
    COLOREDLOGS_LEVEL_STYLES='info=220;spam=22;debug=34;verbose=34;notice=220;warning=202;success=118,bold;error=124;critical=background=red' \
    COLOREDLOGS_LOG_FORMAT='%(asctime)s [%(name)s] %(levelname)s %(message)s'


# Expose port 8501 for Streamlit
EXPOSE 8502

# Healthcheck to ensure the Streamlit server is running
HEALTHCHECK CMD curl --fail http://localhost:8502/_stcore/health

# Run the Streamlit application
ENTRYPOINT ["poetry", "run", "python", "-m", "streamlit", "run", "chat_master.py", "--server.port=8502", "--server.address=0.0.0.0"]
