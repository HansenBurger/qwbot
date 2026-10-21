FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 国内/受限网络下容器直连 pypi.org 会失败，默认指向镜像源；
# 需要官方源时用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

RUN python -m pip install --upgrade pip setuptools wheel

COPY pyproject.toml README.md ./
COPY qwbot ./qwbot

RUN pip install .

EXPOSE 5000

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "qwbot.wsgi:app"]
