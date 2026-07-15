# CODEX AGENT: This looks like a developer-local terminal transcript accidentally committed to master:
# it embeds a private Jupyter URL and home paths, executes that URL as a command, deletes directories,
# and lacks a shebang/fail-fast guards. Remove it or replace it with a portable, parameterized build script.
https://jupyterhub-ml-p02.samokat.ru/user/azemlyak/train/proxy/8765/
rm -rf /tmp/ll-ui/
rm -rf ~/data/looplab/ui/dist
cp -r ~/data/looplab/ui /tmp/ll-ui
cd /tmp/ll-ui && npm ci && npm run build
cp -r /tmp/ll-ui/dist ~/data/looplab/ui/dist
cd ~/data/looplab
looplab ui
