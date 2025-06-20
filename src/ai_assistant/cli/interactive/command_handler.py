from . import actions, display, stubs

class CommandHandler:
    def __init__(self, session):
        self.session = session
        self.console = display.console

    async def handle(self, command_str: str):
        """Parse and dispatch slash commands."""
        parts = command_str[1:].split()
        cmd = parts[0].lower()
        args = parts[1:]
        
        try:
            if cmd == 'file':
                if not args:
                    self.console.print("[red]Error: Missing file path[/red]")
                    return
                await actions.add_file_to_context(self.session, args[0])
                
            elif cmd == 'refresh':
                self.console.print("[yellow]Refreshing repository context...[/yellow]")
                await actions.refresh_repo_context(self.session)
                
            elif cmd == 'clear':
                actions.clear_history(self.session)
                
            elif cmd == 'files':
                display.list_files_in_context(self.session.current_files)
                
            elif cmd == 'repo':
                await actions.show_repository_stats(self.session)
                
            elif cmd == 'model' and args:
                actions.switch_model(self.session, args[0])
                
            elif cmd == 'save_conversation' and args:
                await actions.save_conversation(self.session, args[0])
                
            elif cmd == 'new' and args:
                await stubs.handle_new_file(self.session, args[0])
                
            elif cmd == 'save' and args:
                await stubs.handle_save_last_code(self.session, args[0])
                
            elif cmd == 'git_add' and args:
                await stubs.handle_git_add(self.session, args)
                
            elif cmd == 'git_commit' and args:
                commit_message = ' '.join(args)
                await stubs.handle_git_commit(self.session, commit_message)
                
            elif cmd == 'git_push':
                await stubs.handle_git_push(self.session)
                
            else:
                self.console.print(f"[red]Unknown command: /{cmd}[/red]")
                display.show_help()
                
        except Exception as e:
            import traceback
            self.console.print(f"[red]Error executing command: {e}[/red]")
            self.console.print(f"[dim]{traceback.format_exc()}[/dim]")