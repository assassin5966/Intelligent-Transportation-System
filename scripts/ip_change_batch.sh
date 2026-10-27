#!/bin/bash
OLD="23.45.1.115"
NEW="10.10.10.11"

# 改 4 个必动位置
sed -i "s/${OLD}/${NEW}/g" .env.gpu
sed -i "s/${OLD}/${NEW}/g" setting-server-gpu/application.yml
sed -i "s/${OLD}/${NEW}/g" scripts/start_all_gpu.sh

# 可选:同步 .env + 老备份
sed -i "s/${OLD}/${NEW}/g" .env
#sed -i "s/${OLD}/${NEW}/g" wvp_zlm.yml          # 若还在用
#sed -i "s/${OLD}/${NEW}/g" setting-server-bak/application.yml  # 冷备份

