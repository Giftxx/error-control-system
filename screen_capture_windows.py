"""
Multi-Window Capture - จับภาพแยกแต่ละหน้าต่างที่เปิดอยู่ ทุก 3 วินาที
"""
import os
import time
import ctypes
from datetime import datetime
from PIL import Image
import win32gui
import win32ui
import win32con
import re

# Enable DPI Awareness
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except:
        pass


def get_all_windows():
    """ค้นหาหน้าต่างทั้งหมดที่เปิดอยู่"""
    windows = []
    
    def enum_callback(hwnd, results):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                # ข้ามหน้าต่างระบบ
                skip_titles = ['Program Manager', 'Settings', 'Microsoft Text Input Application', 
                              'Windows Input Experience', 'NVIDIA GeForce Overlay', 'Microsoft Store']
                skip_contains = ['Windows', 'Taskbar', 'Task Switching']
                
                if title not in skip_titles and not any(s in title for s in skip_contains):
                    rect = win32gui.GetWindowRect(hwnd)
                    width = rect[2] - rect[0]
                    height = rect[3] - rect[1]
                    if width > 200 and height > 200:
                        results.append({
                            'hwnd': hwnd,
                            'title': title,
                            'rect': rect,
                            'width': width,
                            'height': height
                        })
        return True
    
    win32gui.EnumWindows(enum_callback, windows)
    return windows


def capture_window(hwnd):
    """จับภาพหน้าต่างเดียว"""
    try:
        rect = win32gui.GetWindowRect(hwnd)
        x, y, x2, y2 = rect
        width = x2 - x
        height = y2 - y
        
        if width <= 0 or height <= 0:
            return None
        
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        
        # PrintWindow
        result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        
        if result == 0:
            save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)
        
        bmp_info = bitmap.GetInfo()
        bmp_str = bitmap.GetBitmapBits(True)
        
        img = Image.frombuffer(
            'RGB',
            (bmp_info['bmWidth'], bmp_info['bmHeight']),
            bmp_str, 'raw', 'BGRX', 0, 1
        )
        
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        win32gui.DeleteObject(bitmap.GetHandle())
        
        return img
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def sanitize_filename(title, max_length=40):
    """แปลงชื่อหน้าต่างให้เป็นชื่อไฟล์"""
    title = re.sub(r'[<>:"/\\|?*]', '', title)
    title = title.strip()
    if len(title) > max_length:
        title = title[:max_length]
    return title if title else "unknown"


def get_app_folder_name(title):
    """แยกชื่อแอป/โปรแกรมจากชื่อหน้าต่างเพื่อใช้เป็นชื่อโฟลเดอร์"""
    title_lower = title.lower()
    
    # กำหนดชื่อโฟลเดอร์ตามแอปพลิเคชัน
    if 'visual studio code' in title_lower or 'vs code' in title_lower:
        return 'Visual_Studio_Code'
    elif 'google chrome' in title_lower:
        return 'Google_Chrome'
    elif 'firefox' in title_lower:
        return 'Firefox'
    elif 'microsoft edge' in title_lower:
        return 'Microsoft_Edge'
    elif 'webex' in title_lower:
        return 'Webex'
    elif 'zoom' in title_lower:
        return 'Zoom'
    elif 'teams' in title_lower:
        return 'Microsoft_Teams'
    elif 'word' in title_lower or '.docx' in title_lower or '.doc' in title_lower:
        return 'Microsoft_Word'
    elif 'excel' in title_lower or '.xlsx' in title_lower or '.xls' in title_lower:
        return 'Microsoft_Excel'
    elif 'powerpoint' in title_lower or '.pptx' in title_lower or '.ppt' in title_lower:
        return 'Microsoft_PowerPoint'
    elif '.pdf' in title_lower or 'adobe' in title_lower:
        return 'PDF_Viewer'
    elif 'notepad' in title_lower:
        return 'Notepad'
    elif 'explorer' in title_lower or 'file explorer' in title_lower:
        return 'File_Explorer'
    elif 'realtek' in title_lower:
        return 'Realtek_Audio'
    elif '.png' in title_lower or '.jpg' in title_lower or '.jpeg' in title_lower:
        return 'Image_Viewer'
    else:
        # ใช้คำแรกหรือชื่อหลักจาก title
        clean_title = re.sub(r'[<>:"/\\|?*]', '', title)
        words = clean_title.split()
        if words:
            # ใช้คำแรกที่มีความหมาย (ไม่ใช่สัญลักษณ์)
            for word in words[:3]:
                if len(word) > 2 and word.isalnum():
                    return word[:20]
        return 'Other'


def main():
    base_dir = "screen_data/captures/Windows"
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)
    
    print("\n🖼️  Multi-Window Capture - จับภาพแยกโฟลเดอร์ตามแถบ ทุก 30 วินาที")
    print("=" * 60)
    print(f"📁 บันทึกที่: {os.path.abspath(base_dir)}")
    print("📂 แยกโฟลเดอร์อัตโนมัติตามชื่อแอป")
    print("⏹️  กด Ctrl+C เพื่อหยุด\n")
    
    count = 0
    
    try:
        while True:
            windows = get_all_windows()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            if windows:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] พบ {len(windows)} หน้าต่าง:")
                
                for i, w in enumerate(windows, 1):
                    img = capture_window(w['hwnd'])
                    if img:
                        # สร้างโฟลเดอร์ตามชื่อแอป
                        folder_name = get_app_folder_name(w['title'])
                        app_dir = os.path.join(base_dir, folder_name)
                        if not os.path.exists(app_dir):
                            os.makedirs(app_dir)
                            print(f"   📂 สร้างโฟลเดอร์: {folder_name}")
                        
                        title_clean = sanitize_filename(w['title'])
                        filename = f"{timestamp}_{title_clean}.png"
                        filepath = os.path.join(app_dir, filename)
                        img.save(filepath)
                        count += 1
                        
                        short_title = w['title'][:35] + "..." if len(w['title']) > 35 else w['title']
                        print(f"   ✓ [{folder_name}] {short_title}")
                
                print(f"   → รวม {count} ภาพ\n")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ไม่พบหน้าต่าง\n")
            
            time.sleep(30)
            
    except KeyboardInterrupt:
        print(f"\n🛑 หยุดแล้ว! รวมทั้งหมด {count} ภาพ")
        print(f"📁 ไฟล์อยู่ที่: {os.path.abspath(base_dir)}")
        
        # แสดงสรุปโฟลเดอร์ทั้งหมด
        print("\n📊 สรุปโฟลเดอร์ที่สร้าง:")
        if os.path.exists(base_dir):
            for folder in os.listdir(base_dir):
                folder_path = os.path.join(base_dir, folder)
                if os.path.isdir(folder_path):
                    file_count = len([f for f in os.listdir(folder_path) if f.endswith('.png')])
                    print(f"   📂 {folder}: {file_count} ภาพ")


if __name__ == "__main__":
    main()
