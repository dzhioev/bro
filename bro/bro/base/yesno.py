YES = True
NO = False

def yesno(question: str, default=NO) -> bool:
    if default == YES:
        prompt = f"{question} [Y/n]: "
        default_char = 'Y'
    else:
        prompt = f"{question} [y/N]: "
        default_char = 'N'
    
    while True:
        try:
            response = input(prompt).strip().lower()
            
            if response == '':
                return default
            elif response in ['y', 'yes']:
                return True
            elif response in ['n', 'no']:
                return False
            else:
                print("Please enter 'y(es)' or 'n(o)' (or press Enter for default)")
        except KeyboardInterrupt:
            print("\nInterrupted!")
            raise
        except EOFError:
            print(f"\n{default_char}")
            return default
