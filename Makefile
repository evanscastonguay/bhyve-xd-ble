# bhyve-xd-ble — dev + deploy shortcuts.
.PHONY: test deploy release rollback run

test:            ## run the hardware-free test suite
	./venv/bin/python -m pytest test_e2e.py -q

run:             ## run the server locally (localhost only)
	./venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000

deploy:          ## push the current HEAD to the Linux box (test-gated, health-checked, auto-rollback)
	./deploy/deploy.sh

release: deploy  ## alias for deploy (tag first with `git tag vX.Y.Z && git push --tags` if you want a marker)

rollback:        ## flip the box back to the previous release
	./deploy/rollback.sh
