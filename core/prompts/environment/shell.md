# Shell 与命令执行

- **Shell 语法：** 'execute_command' 的默认执行器是 ^{shell_hint}^，命令必须按它的语法书写：cmd.exe 下顺序执行用 `&&`（**不要用 `;`**，它只是普通字符），命令必须是**单行**（换行会截断命令，只执行到第一个换行），不要用 heredoc。host 会按执行器前置做语法检查并给出改法；需要另一种执行器时可传 `shell` 参数单次指定（git_bash / powershell / cmd），例如要多行脚本、heredoc 或 PowerShell cmdlet 时改用 git_bash / powershell。命令返回"无输出"说明它真的没有产出（不是回显被吞）；若怀疑命令没按预期执行，先核对语法与执行器再改写法。
