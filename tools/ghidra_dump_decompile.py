# Ghidra Jython script. Run with analyzeHeadless -postScript.

from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor


args = getScriptArgs()
if not args:
    raise Exception("usage: ghidra_dump_decompile.py <address> [<address> ...]")

monitor = ConsoleTaskMonitor()
decompiler = DecompInterface()
decompiler.openProgram(currentProgram)
functions = currentProgram.getFunctionManager()

seen = set()
for arg in args:
    address = toAddr(arg)
    function = functions.getFunctionContaining(address)
    if function is None:
        print("NO_FUNCTION %s" % arg)
        continue
    entry = function.getEntryPoint().toString()
    if entry in seen:
        continue
    seen.add(entry)
    print("===== FUNCTION %s %s =====" % (function.getName(), entry))
    result = decompiler.decompileFunction(function, 120, monitor)
    if result.decompileCompleted():
        print(result.getDecompiledFunction().getC())
    else:
        print("DECOMPILE_FAILED %s" % result.getErrorMessage())
