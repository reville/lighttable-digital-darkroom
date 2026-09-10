#!/usr/bin/env bash
# Disposable full-system VM; its SSH port listens only on runner loopback.
set -euo pipefail
mkdir -p evidence baseline/dist /tmp/lighttable-vm
package=lighttable-bin-0.6.0-1-x86_64.pkg.tar.zst
test -f "baseline/dist/$package"
(cd baseline && sha256sum -c dist/SHA256SUMS)
sha256sum "baseline/dist/$package" > evidence/tested-package.sha256
vm=/tmp/lighttable-vm
image=Arch-Linux-x86_64-cloudimg.qcow2
curl -fsSL --retry 2 --max-time 180 "https://geo.mirror.pkgbuild.com/images/latest/$image" -o "$vm/$image"
curl -fsSL --retry 2 --max-time 30 "https://geo.mirror.pkgbuild.com/images/latest/$image.SHA256" -o "$vm/$image.SHA256"
(cd "$vm" && sha256sum -c "$image.SHA256")
cp "$vm/$image.SHA256" evidence/cloud-image.sha256
qemu-img resize "$vm/$image" 24G
ssh-keygen -q -t ed25519 -N '' -f "$vm/id"
cat > "$vm/user-data" <<EOF
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
      - $(cat "$vm/id.pub")
runcmd:
  - [systemctl, enable, --now, sshd]
EOF
printf 'instance-id: lighttable-arch-release\nlocal-hostname: lighttable-release\n' > "$vm/meta-data"
cloud-localds "$vm/seed.img" "$vm/user-data" "$vm/meta-data"
cp /usr/share/OVMF/OVMF_VARS_4M.fd "$vm/vars.fd"
sudo qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -m 8192 -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file="$vm/vars.fd" \
  -drive file="$vm/$image",if=virtio,format=qcow2 \
  -drive file="$vm/seed.img",if=virtio,format=raw \
  -device virtio-vga -display none -serial "file:$PWD/evidence/serial.log" \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=net0 \
  -daemonize -pidfile "$vm/qemu.pid"
remote=(ssh -i "$vm/id" -p 2222 -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1)
cleanup() {
  "${remote[@]}" 'tar -C /work -czf - evidence' > "$vm/evidence.tar.gz" 2>/dev/null && tar -xzf "$vm/evidence.tar.gz" || true
  if [[ -f "$vm/qemu.pid" ]]; then sudo kill "$(sudo cat "$vm/qemu.pid")" 2>/dev/null || true; fi
  sudo chown -R "$(id -u):$(id -g)" evidence
  if [[ -f evidence/guest.log ]]; then tail -45 evidence/guest.log; fi
  if [[ -f evidence/hyprland/detail.log ]]; then tail -35 evidence/hyprland/detail.log; fi
}
trap cleanup EXIT
for attempt in $(seq 1 90); do
  if "${remote[@]}" true 2>/dev/null; then break; fi
  sleep 2
done
"${remote[@]}" 'cloud-init status --wait; mkdir -p /work/dist /work/evidence; ls -l /dev/dri; uname -a'
tar -czf - --exclude=.git --exclude=baseline --exclude=evidence . | "${remote[@]}" 'tar -xzf - -C /work'
cat baseline/dist/lighttable-bin-0.6.0-1-x86_64.pkg.tar.zst | "${remote[@]}" 'cat > /work/dist/lighttable-bin-0.6.0-1-x86_64.pkg.tar.zst'
"${remote[@]}" "cd /work && DISTRIBUTION=$DISTRIBUTION VM_GPU=1 REUSE_PACKAGE=1 bash scripts/linux/arch-release-container.sh" > evidence/guest.log 2>&1
