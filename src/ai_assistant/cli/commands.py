"""
CLI command implementations
"""
import asyncio
import logging
import re
import click
import os
from pathlib import Path
from typing import List, Optional, Dict
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich.live import Live
from rich.spinner import Spinner

from ..core.config import Config
from ..core.exceptions import AIAssistantError, FileServiceError, GitHubServiceError
from ..services.ai_service import AIService
from ..services.github_service import GitHubService
from ..services.file_service import FileService
from ..models.request import CodeRequest
from ..models.response import CodeResponse
from ..utils.file_utils import FileUtils
from ..utils.git_utils import GitUtils

console = Console()
logger = logging.getLogger(__name__)

class CodeCommands:
    """Implementation of code-related commands"""
    
    def __init__(self, config: Config):
        self.config = config
        self.file_service = FileService(config)
        self.file_utils = FileUtils()
        # GitHubService is initialized with the current working directory
        self.github_service = GitHubService(config, Path.cwd())

    async def get_ai_repo_summary(self, repo_path: Path = None) -> str:
        github_service = GitHubService(self.config, repo_path)
        return await github_service.get_ai_repo_summary(repo_path)

    async def generate_code(self, prompt: str, files: List[str], 
                          show_diff: bool = False, apply_changes: bool = False):
        """Generate or modify code based on a prompt and file context."""
        ai_service = None # Initialize to None for the finally block
        try:
            request = await self._prepare_request(prompt, files)
            
            async with AIService(self.config) as ai_service:
                response_content = ""
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    transient=True,
                    console=console,
                ) as progress:
                    progress.add_task(f"Asking {self.config.get_current_model().name}...", total=None)
                    response_content = ""
                    async for chunk in ai_service.stream_generate(request):
                        response_content += chunk
            
                await self._display_and_process_response(response_content, show_diff, apply_changes)

        except Exception as e:
            logger.error(f"Error during code generation: {e}", exc_info=True)
            raise AIAssistantError(f"Failed to generate code: {e}")
        finally:
            pass # Or remove the finally block if it becomes empty and ai_service is only used in try.

    async def review_changes(self, create_branch: Optional[str] = None,
                           commit_changes: bool = False, push_changes: bool = False):
        """Review staged changes and optionally commit and push them after verification."""
        try:
            from ai_assistant.utils.git_utils import GitUtils
            git_utils = GitUtils()
            repo_context = await self.github_service.get_repository_context()
            if not repo_context.get("is_git_repo"):
                raise AIAssistantError("Not a Git repository. Cannot review changes.")

            # 1. Check if there are staged changes.
            staged_diff = await self.github_service.get_staged_diff()
            if not staged_diff:
                # Check for any modified/untracked files using "git status --porcelain"
                proc = await asyncio.create_subprocess_shell(
                    f"git -C {Path.cwd()} status --porcelain",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                status_output = stdout.decode().strip()
                if not status_output:
                    console.print("[yellow]No modified or untracked files found in the repository.[/yellow]")
                    return
                else:
                    console.print("[yellow]Detected modified/untracked files in the repository.[/yellow]")
                    if click.confirm("Would you like to stage all changes (including untracked files)?", default=True):
                        repo_path = Path.cwd()
                        await git_utils.add_all(repo_path)
                        console.print("[green]All changes have been staged.[/green]")
                        staged_diff = await self.github_service.get_staged_diff()
                        if not staged_diff:
                            console.print("[red]No changes were staged even after staging. Aborting review process.[/red]")
                            return
                    else:
                        console.print("[yellow]No changes were staged. Aborting review process.[/yellow]")
                        return

            # 2. Display the staged diff for review.
            console.print(Panel(
                Syntax(staged_diff, "diff", theme="github-dark", word_wrap=True),
                title="Staged Changes for Review",
                border_style="yellow"
            ))

            # Auto generate commit message based on staged_diff.
            if click.confirm("Do you want to commit the staged changes?", default=True):
                commit_message = click.prompt("Enter commit message", default="Update via AI Assistant")
                await git_utils.commit(Path.cwd(), commit_message)
                console.print(f"[green]✓ Changes committed with message: {commit_message}[/green]")

                if click.confirm("Do you want to push these changes to remote?", default=True):
                    with Live(Spinner("dots", text="Pushing changes..."), refresh_per_second=4, console=console):
                        branch = await git_utils.get_current_branch(Path.cwd())
                        await git_utils.push(Path.cwd(), branch)
                    console.print(f"[green]✓ Changes pushed to branch {branch}.[/green]")
                else:
                    console.print("[yellow]Changes committed locally but not pushed.[/yellow]")
            else:
                console.print("[yellow]Staging complete. No commit operation was triggered.[/yellow]")

        except Exception as e:
            logger.error(f"Error during review process: {e}", exc_info=True)
            raise AIAssistantError(f"Failed to review changes: {e}")

    async def _prepare_request(self, prompt: str, files: List[str]) -> CodeRequest:
        """Prepare AI request with file and Git context."""
        file_contents = {}
        git_context_str = ""
        
        # Load file contents concurrently
        read_tasks = [self.file_service.read_file(Path(file)) for file in files]
        results = await asyncio.gather(*read_tasks, return_exceptions=True)
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                console.print(f"[yellow]Warning: Could not read {files[i]}: {result}[/yellow]")
            else:
                file_contents[files[i]] = result

        # Get Git context if in a repo
        repo_context = await self.github_service.get_repository_context()   
        if repo_context.get("is_git_repo"):
            try:
                git_context_str = (
                    f"Current Branch: {repo_context.get('current_branch')}\n"
                    f"Unstaged Changes:\n{repo_context.get('status') or 'None'}"
                )
            except GitHubServiceError as e:
                console.print(f"[yellow]Warning: Could not get Git context: {e}[/yellow]")

        return CodeRequest(
            prompt=prompt,
            files=file_contents,
            git_context=git_context_str
        )

    async def _display_and_process_response(self, content: str, show_diff: bool, apply_changes: bool):
        """Display AI response and handle diff/apply logic."""
        console.print(Panel(
            Syntax(content, "markdown", theme="github-dark", word_wrap=True),
            title=f"AI Response ({self.config.get_current_model().name})",
            border_style="blue"
        ))

        code_blocks = self._extract_code_blocks(content)
        if not code_blocks:
            # No file-specific code blocks found; prompt to save the full response instead.
            if click.confirm("No file paths detected in the AI response. Would you like to save the full response to a file?", default=True):
                file_name = click.prompt("Enter file name", default="ai_suggestion.txt")
                await self.file_service.write_file(Path(file_name), content)
                console.print(f"[green]Response saved to {file_name}.[/green]")
            else:
                console.print("[yellow]No changes applied.[/yellow]")
            return

        # Ask to apply changes (unless auto-applying via --apply flag)
        if not apply_changes:
            if not click.confirm("Do you want to apply these changes to your files?", default=True):
                console.print("[yellow]Changes were not applied.[/yellow]")
                return
        else:
            console.print("[green]Auto-applying changes as per --apply flag.[/green]")

        # Optionally show diff and apply changes to each file
        for file_path_str, code in code_blocks.items():
            file_path = Path(file_path_str)
            if show_diff:
                await self._show_file_diff(file_path, code)
            await self._apply_code_changes(file_path, code)
        console.print("[green]✓ Changes applied.[/green]")

        # Git Integration: Initialize repo if not present, then commit and push
        repo_path = Path.cwd()
        git_utils = GitUtils()
        if not await git_utils.is_git_repo(repo_path):
            if click.confirm("This directory is not a Git repository. Do you want to initialize a git repo here?", default=True):
                await git_utils.initialize_repository(repo_path)
                console.print("[green]✓ Git repository initialized.[/green]")
            else:
                console.print("[yellow]Git repository was not initialized.[/yellow]")
                return

        if click.confirm("Do you want to commit and push these changes to git?", default=False):
            for file_path_str in code_blocks:
                await git_utils.add_file(repo_path, file_path_str)
            commit_message = click.prompt("Enter commit message", default="Update via AI Assistant")
            await git_utils.commit(repo_path, commit_message)
            branch = await git_utils.get_current_branch(repo_path)
            await git_utils.push(repo_path, branch)
            console.print(f"[green]✓ Changes committed and pushed to branch {branch}.[/green]")
        else:
            console.print("[yellow]Changes were not pushed to git.[/yellow]")

    def _extract_code_blocks(self, content: str) -> Dict[str, str]:
        """Extracts code blocks that have a file path specified in the language hint."""
        # Regex to find ```language:path/to/file.ext
        # It captures the path and the code content until the closing ```
        pattern = re.compile(r"```(?:\w+:)?(.+?)\n(.*?)\n```", re.DOTALL)
        matches = pattern.findall(content)
        
        code_blocks = {}
        for match in matches:
            path, code = match
            # Clean up potential extra characters around path
            file_path = path.strip()
            # The system prompt requests a path, so we assume it's a file path
            if '/' in file_path or '\\' in file_path or '.' in file_path:
                 code_blocks[file_path] = code.strip()

        return code_blocks

    async def _show_file_diff(self, file_path: Path, new_code: str):
        """Displays a colorized diff for a file's changes."""
        try:
            if file_path.exists():
                original_code = await self.file_service.read_file(file_path)
                diff = self.file_utils.generate_diff(original_code, new_code, str(file_path))
                panel_title = f"Diff for {file_path}"
                border_style = "yellow"
                syntax_lang = "diff"
            else:
                diff = new_code
                panel_title = f"New File: {file_path}"
                border_style = "green"
                syntax_lang = self.file_utils.get_language_from_extension(file_path.suffix)

            console.print(Panel(
                Syntax(diff, syntax_lang, theme="github-dark", word_wrap=True),
                title=panel_title,
                border_style=border_style
            ))

        except (FileServiceError, Exception) as e:
            console.print(f"[red]Error showing diff for {file_path}: {e}[/red]")
    
    async def _apply_code_changes(self, file_path: Path, code: str):
        """Applies the provided code to the specified file."""
        try:
            await self.file_service.write_file(file_path, code)
            console.print(f"[green]✓ Applied changes to {file_path}[/green]")
        except FileServiceError as e:
            console.print(f"[red]Error applying changes to {file_path}: {e}[/red]")

    async def _generate_commit_message(self, diff: str) -> str:

        """Uses the AI to generate a conventional commit message from a diff."""
        commit_message = ""
        # Create a CodeRequest using the diff as the prompt.
        request = CodeRequest(prompt=diff, files={}, git_context="")
        async with AIService(self.config) as ai_service:
            async for chunk in ai_service.stream_generate(request):
                commit_message += chunk
        return commit_message.strip()
    
    @staticmethod
    def build_repo_context(repo_path):
        """
        Recursively collect the content of all text files in the given repository directory.
        Files in directories such as .git, node_modules, or __pycache__ are skipped.
        """
        context = {}
        for root, dirs, files in os.walk(repo_path):
            # Exclude directories that are not needed
            dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__']]
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    # Only read text files, skipping those that cannot be decoded
                    with open(file_path, 'r', encoding='utf-8') as f:
                        context[file_path] = f.read()
                except Exception as e:
                    # Could log the exception if needed
                    pass
        return context

    @staticmethod
    def create_context():
        """
        Build context for the entire local repository.
        Assumes the current working directory is the repository root.
        """
        repo_path = os.getcwd()
        return CodeCommands.build_repo_context(repo_path)

@click.group(invoke_without_command=True)
@click.option('--model', default=None, help="Model to use")
@click.pass_context
def helios(ctx, model):
    """
    Helios CLI entry point.
    If no subcommand is provided, prompt the user to select a model interactively.
    """
    # Initialize configuration (adjust this if you load config differently)
    config = Config()
    ctx.obj = {"config": config}
    if model is None and ctx.invoked_subcommand is None:
        # Prompt user to pick a model from available models (assumes get_available_models returns a list)
        available_models = config.get_available_models()
        model_choice = click.prompt("Select a model", type=click.Choice(available_models))
        config.set_current_model(model_choice)
        click.echo(f"Model set to {model_choice}")
        # Begin interactive command loop
        while True:
            cmd = click.prompt("helios>", prompt_suffix=" ")
            if cmd.lower() in ["exit", "quit"]:
                break
            elif cmd.lower() == "chat":
                ctx.invoke(chat)
            elif cmd.lower() == "review":
                ctx.invoke(review)
            elif cmd.lower() == "repo-summary":
                click.echo("Repo-summary mode not implemented yet.")
            else:
                click.echo(f"Unknown command: {cmd}")

@helios.command()
def chat():
    """
    Chat mode command.
    """
    config = click.get_current_context().obj.get("config")
    code_cmds = CodeCommands(config)
    click.echo(f"Starting chat mode with model: {config.get_current_model()}")
    # Place chat mode implementation here.

@helios.command()
def review():
    """
    Review changes command.
    """
    config = click.get_current_context().obj.get("config")
    code_cmds = CodeCommands(config)
    click.echo(f"Starting review mode with model: {config.get_current_model()}")
    # Place review mode implementation here.

if __name__ == '__main__':
    helios()