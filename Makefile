# make check runs offline and free. make run calls the OpenAI API and costs money (about 1 cent a task).
.PHONY: check analyze run

check:
	python3 -m unittest discover -s tests -v
	python3 analyze.py --check

analyze:
	python3 analyze.py

# make run writes a new file in data/raw/ and prints the analyze.py command for it alone.
run:
	python3 trace_agent.py --task checkout --model gpt-6.2-sol --efforts medium xhigh --runs 10 --max-cost 5
