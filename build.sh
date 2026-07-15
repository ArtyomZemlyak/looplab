https://jupyterhub-ml-p02.samokat.ru/user/azemlyak/train/proxy/8765/
rm -rf /tmp/ll-ui/
rm -rf ~/data/looplab/ui/dist
cp -r ~/data/looplab/ui /tmp/ll-ui
cd /tmp/ll-ui && npm ci && npm run build
cp -r /tmp/ll-ui/dist ~/data/looplab/ui/dist
cd ~/data/looplab 
looplab ui