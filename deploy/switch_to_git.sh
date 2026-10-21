set -e
export DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null 2>&1 || apt-get install -y git

systemctl stop dream-monitor || true
cd /home/ubuntu
OLD="dream-monitor.old.$(date +%Y%m%d%H%M%S)"
mv dream-monitor "$OLD"

git clone --quiet https://github.com/LinXiaoAn7/dream-monitor.git /home/ubuntu/dream-monitor

cp "$OLD/config.yaml" /home/ubuntu/dream-monitor/config.yaml
mkdir -p /home/ubuntu/dream-monitor/data
cp "$OLD/data/dm.db" /home/ubuntu/dream-monitor/data/dm.db

chown -R ubuntu:ubuntu /home/ubuntu/dream-monitor

systemctl start dream-monitor
sleep 5
echo "---- 服务状态 ----"
systemctl is-active dream-monitor
echo "---- 备份目录 ----"
echo "$OLD"
echo "---- git 信息 ----"
sudo -u ubuntu git -C /home/ubuntu/dream-monitor log --oneline -1
sudo -u ubuntu git -C /home/ubuntu/dream-monitor remote -v
echo "---- 保留文件 ----"
ls -la /home/ubuntu/dream-monitor/config.yaml /home/ubuntu/dream-monitor/data/dm.db
