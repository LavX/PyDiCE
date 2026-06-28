// Ghidra Java script. Run with analyzeHeadless -postScript.
//@category pyobd

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.MemoryAccessException;
import ghidra.program.model.data.PointerDataType;

public class GhidraDumpVtable extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length == 0) {
            throw new IllegalArgumentException("usage: GhidraDumpVtable <address> [<address> ...]");
        }

        Listing listing = currentProgram.getListing();
        for (String arg : args) {
            Address address = toAddr(arg);
            println("===== VTABLE " + address + " =====");
            for (int i = 0; i < 6; i++) {
                Address entry = address.add((long) i * currentProgram.getDefaultPointerSize());
                try {
                    long target = getCurrentProgram().getMemory().getLong(entry);
                    println("PTR[" + i + "] " + entry + " -> 0x" + Long.toHexString(target));
                    Function function = currentProgram.getFunctionManager().getFunctionAt(toAddr(target));
                    if (function != null) {
                        println("FUNC " + function.getEntryPoint() + " " + function.getName());
                    }
                } catch (MemoryAccessException ex) {
                    println("PTR[" + i + "] " + entry + " unreadable: " + ex.getMessage());
                }
            }
        }
    }
}
