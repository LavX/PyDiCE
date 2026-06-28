// Ghidra Java script. Run with analyzeHeadless -postScript.
//@category pyobd

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

import java.util.HashSet;
import java.util.Set;

public class GhidraDumpDecompile extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length == 0) {
            throw new IllegalArgumentException("usage: GhidraDumpDecompile <address> [<address> ...]");
        }

        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        Set<String> seen = new HashSet<String>();

        for (String arg : args) {
            Address address = toAddr(arg);
            Function function = currentProgram.getFunctionManager().getFunctionContaining(address);
            if (function == null) {
                println("NO_FUNCTION " + arg);
                continue;
            }
            String entry = function.getEntryPoint().toString();
            if (seen.contains(entry)) {
                continue;
            }
            seen.add(entry);
            println("===== FUNCTION " + function.getName() + " " + entry + " =====");
            DecompileResults results = decompiler.decompileFunction(function, 120, monitor);
            if (results.decompileCompleted()) {
                println(results.getDecompiledFunction().getC());
            } else {
                println("DECOMPILE_FAILED " + results.getErrorMessage());
            }
        }
    }
}
