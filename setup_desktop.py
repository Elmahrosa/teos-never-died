import os
import sys
import subprocess

try:
    import winshell
    from win32com.client import Dispatch
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

def get_desktop_path():
    """Get the path to the current user's Windows Desktop"""
    return os.path.join(os.environ['USERPROFILE'], 'Desktop')

def create_vbs_script(desktop_path):
    """Create Start_RAVEN_Hidden.vbs on desktop"""
    vbs_content = '''Set WshShell = WScript.CreateObject("WScript.Shell")
WshShell.Run chr(34) & "D:\\elmahrosa-repos\\teos-never-died\\Start_RAVEN_Bot.bat" & chr(34), 0
Set WshShell = Nothing'''
    
    vbs_path = os.path.join(desktop_path, "Start_RAVEN_Hidden.vbs")
    with open(vbs_path, 'w') as f:
        f.write(vbs_content)
    return vbs_path

def create_stop_batch(desktop_path):
    """Create Stop_RAVEN.bat on desktop"""
    bat_content = '''@echo off
taskkill /F /IM python.exe /T
echo RAVEN processes terminated.'''
    
    bat_path = os.path.join(desktop_path, "Stop_RAVEN.bat")
    with open(bat_path, 'w') as f:
        f.write(bat_content)
    return bat_path

def create_shortcut(desktop_path, vbs_path):
    """Create RAVEN_24_7_Shortcut.lnk on desktop pointing to the VBS script"""
    if not WIN32_AVAILABLE:
        print("⚠️  Windows shortcut creation requires pywin32")
        print("    Install with: pip install pywin32")
        return None
        
    shell = Dispatch('WScript.Shell')
    shortcut_path = os.path.join(desktop_path, "RAVEN_24_7_Shortcut.lnk")
    shortcut = shell.CreateShortCut(shortcut_path)
    shortcut.Targetpath = vbs_path
    shortcut.WorkingDirectory = os.path.dirname(vbs_path)
    shortcut.IconLocation = "shell32.dll,13"  # Use a default Windows icon
    shortcut.save()
    return shortcut_path

def main():
    print("🚀 Creating RAVEN desktop shortcuts...")
    
    try:
        # Get desktop path
        desktop_path = get_desktop_path()
        print(f"📁 Desktop path: {desktop_path}")
        
        # Create VBS script
        vbs_path = create_vbs_script(desktop_path)
        print(f"✅ Created: {os.path.basename(vbs_path)}")
        
        # Create stop batch file
        bat_path = create_stop_batch(desktop_path)
        print(f"✅ Created: {os.path.basename(bat_path)}")
        
        # Create shortcut
        shortcut_path = create_shortcut(desktop_path, vbs_path)
        if shortcut_path:
            print(f"✅ Created: {os.path.basename(shortcut_path)}")
        
        print("\n🎉 All shortcuts created successfully!")
        print("📝 Double-click 'RAVEN_24_7_Shortcut.lnk' on your desktop to start RAVEN silently.")
        print("🛑 Use 'Stop_RAVEN.bat' to terminate RAVEN processes.")
        
    except Exception as e:
        print(f"❌ Error creating shortcuts: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())