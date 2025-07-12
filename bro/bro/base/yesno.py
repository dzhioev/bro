import sys
import tty
import termios

YES = True
NO = False

def getch():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1).lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

def yesno(question: str, default=NO) -> bool:
    if default == YES:
        prompt = f"{question} [Y/n]: "
    else:
        prompt = f"{question} [y/N]: "
    
    while True:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        
        key = getch()
        
        if key in ['\r', '\n']:
            if default == YES:
                sys.stdout.write('y\n')
            else:
                sys.stdout.write('n\n')
            return default
        elif key == 'y':
            sys.stdout.write('y\n')
            return True
        elif key == 'n':
            sys.stdout.write('n\n')
            return False
        
        sys.stdout.write('\n')
