from datetime import timedelta

def format_time(seconds):
    """Format seconds into human-readable time"""
    time = timedelta(seconds=seconds)
    days = time.days
    hours, remainder = divmod(time.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    
    return " ".join(parts) if parts else "0s"

def truncate_string(string, length=100):
    """Truncate string to specified length"""
    return string[:length] + "..." if len(string) > length else string

def format_number(number):
    """Format number with commas"""
    return f"{number:,}"