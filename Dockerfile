# Runs the demo: simulator, controller and dashboard. It never needs /sys,
# privileges or devices; the hwmon tree it controls is a fake one inside
# the container.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 fancurve
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /root/.cache

USER fancurve
EXPOSE 8790
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/healthz',timeout=2).status==200 else 1)"
CMD ["python", "-m", "fancurve.demo", "--bind", "0.0.0.0", "--port", "8790"]
