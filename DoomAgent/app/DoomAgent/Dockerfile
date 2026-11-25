FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /var/task

RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-cache -r pyproject.toml

COPY . .

EXPOSE 8080
CMD ["python", "main.py"]
