PYTHON ?= python3
export PYTHONPATH := src

.PHONY: demo sim test lint fmt check docker

demo:            ## simulator + controller + dashboard on http://127.0.0.1:8790/
	$(PYTHON) -m fancurve.demo

sim:             ## only the fake hwmon tree, for running the controller by hand
	$(PYTHON) -m fancurve.sim --root ./sim-sysfs

test:
	$(PYTHON) -m pytest

lint:
	ruff check .
	ruff format --check .

fmt:
	ruff format .
	ruff check --fix .

check:           ## validate the example config against the simulator tree
	$(PYTHON) -m fancurve check -c examples/config.sim.json

docker:
	docker build -t server-fan-control .
	docker run --rm -p 127.0.0.1:8790:8790 server-fan-control
