# 国内构建默认走清华源，境外可 --build-arg PIP_INDEX=https://pypi.org/simple
FROM python:3.12-slim

ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=${PIP_INDEX} \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir -i ${PIP_INDEX} uv

WORKDIR /app

# 先只拷依赖清单：改业务代码时这一层仍命中缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY im_bridge ./im_bridge

EXPOSE 80

# 长连接（QQ）+ HTTP 回调（微信）跑在同一个进程里
CMD ["python", "-m", "im_bridge.main"]
