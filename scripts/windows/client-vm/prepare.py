"""Stage only the verified candidate and test scripts for an ephemeral Windows VM."""
from pathlib import Path
import hashlib
import json
import secrets
import shutil
import sys
import xml.etree.ElementTree as ET

version = sys.argv[1]
if version not in ('10', '11'):
    raise SystemExit('Expected Windows 10 or 11')
root = Path('.build/client-vm')
oem = root / 'oem'
oem.mkdir(parents=True, exist_ok=True)
download = Path('.build/windows-client-download')
installer = download / 'LightTable-0.5.1-windows-x64-setup.exe'
expected = 'a8fea5bf5fe7d5fde3fea3af4b20577a59fe887869d8170a67df5d641527ffd1'
with installer.open('rb') as source:
    actual = hashlib.file_digest(source, 'sha256').hexdigest()
if actual != expected:
    raise SystemExit('Signed candidate installer does not match the verified SHA-256')
shutil.copy2(installer, oem / installer.name)
shutil.copytree('scripts/windows', oem / 'scripts', ignore=shutil.ignore_patterns('client-vm', '__pycache__'))
for name in ('guest-test.ps1', 'bootstrap.ps1'):
    shutil.copy2(Path('scripts/windows/client-vm') / name, oem / name)
(oem / 'candidate.json').write_text(json.dumps({
    'version': '0.5.1', 'windows': version, 'installer': installer.name,
    'installer_sha256': expected,
    'source_sha': '2e4d35d64dc09a35ec2262c1ca9c65bfa44c4912',
}), encoding='utf-8')

# Minimal unattended Windows setup: preserve Defender, firewall, UAC and OS
# requirements. No Dockur customization script or security bypass is staged.
ns = 'urn:schemas-microsoft-com:unattend'
wcm = 'http://schemas.microsoft.com/WMIConfig/2002/State'
ET.register_namespace('', ns)
ET.register_namespace('wcm', wcm)
def add(parent, tag, value=None, **attributes):
    item = ET.SubElement(parent, '{'+ns+'}'+tag, attributes)
    if value is not None:
        item.text = str(value)
    return item
def component(parent, name):
    return add(parent, 'component', name=name, processorArchitecture='amd64',
               publicKeyToken='31bf3856ad364e35', language='neutral', versionScope='nonSxS')
def locale(parent, winpe=False):
    c = component(parent, 'Microsoft-Windows-International-Core' + ('-WinPE' if winpe else ''))
    if winpe:
        add(add(c, 'SetupUILanguage'), 'UILanguage', 'en-US')
    for field, value in [('InputLocale','0409:00000409'),('SystemLocale','en-US'),('UILanguage','en-US'),('UserLocale','en-US')]:
        add(c, field, value)
tree = ET.Element('{'+ns+'}unattend')
pe = add(tree, 'settings', **{'pass': 'windowsPE'})
locale(pe, True)
setup = component(pe, 'Microsoft-Windows-Setup')
disk = add(add(setup, 'DiskConfiguration'), 'Disk', **{'{'+wcm+'}action':'add'})
add(disk, 'DiskID', 1)
add(disk, 'WillWipeDisk', 'true')
create = add(disk, 'CreatePartitions')
modify = add(disk, 'ModifyPartitions')
for order, kind, size, label, fmt in [(1,'EFI',128,'System','FAT32'),(2,'MSR',16,None,None),(3,'Primary',None,'Windows','NTFS')]:
    part = add(create, 'CreatePartition', **{'{'+wcm+'}action':'add'})
    add(part, 'Order', order); add(part, 'Type', kind)
    add(part, 'Size' if size else 'Extend', size if size else 'true')
    part = add(modify, 'ModifyPartition', **{'{'+wcm+'}action':'add'})
    add(part, 'Order', order); add(part, 'PartitionID', order)
    if label:
        add(part, 'Label', label); add(part, 'Format', fmt)
    if order == 3:
        add(part, 'Letter', 'C')
image = add(add(setup, 'ImageInstall'), 'OSImage')
install = add(image, 'InstallTo')
add(install, 'DiskID', 1); add(install, 'PartitionID', 3)
metadata = add(add(image, 'InstallFrom'), 'MetaData', **{'{'+wcm+'}action':'add'})
add(metadata, 'Key', '/IMAGE/INDEX'); add(metadata, 'Value', '1')
user = add(setup, 'UserData')
add(user, 'AcceptEula', 'true')
add(user, 'FullName', 'LightTable Test')
add(user, 'Organization', 'Chonkers LLC')
add(add(setup, 'DynamicUpdate'), 'Enable', 'false')
oobe = add(tree, 'settings', **{'pass':'oobeSystem'})
locale(oobe)
shell = component(oobe, 'Microsoft-Windows-Shell-Setup')
password = secrets.token_urlsafe(32)
accounts = add(add(shell, 'UserAccounts'), 'LocalAccounts')
account = add(accounts, 'LocalAccount', **{'{'+wcm+'}action':'add'})
add(account, 'Name', 'LightTableTest'); add(account, 'Group', 'Administrators')
def set_password(parent):
    node = add(parent, 'Password')
    add(node, 'Value', password); add(node, 'PlainText', 'true')
set_password(account)
autologon = add(shell, 'AutoLogon')
add(autologon, 'Username', 'LightTableTest'); add(autologon, 'Enabled', 'true'); add(autologon, 'LogonCount', 1)
set_password(autologon)
settings = add(shell, 'OOBE')
for key in ('HideEULAPage', 'HideOEMRegistrationScreen', 'HideOnlineAccountScreens', 'HideWirelessSetupInOOBE'):
    add(settings, key, 'true')
add(settings, 'ProtectYourPC', 1)
command = add(add(shell, 'FirstLogonCommands'), 'SynchronousCommand', **{'{'+wcm+'}action':'add'})
add(command, 'Order', 1)
add(command, 'CommandLine', 'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\\OEM\\bootstrap.ps1')
add(command, 'Description', 'Run isolated LightTable client acceptance')
ET.ElementTree(tree).write(root / 'custom.xml', encoding='utf-8', xml_declaration=True)
(root / 'custom.xml').chmod(0o600)
print('Staged verified installer and disposable Windows', version, 'configuration')
