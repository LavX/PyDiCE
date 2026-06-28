// Ghidra Java script. Run with analyzeHeadless -postScript.
//@category pyobd

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import ghidra.program.model.symbol.RefType;

import java.util.Iterator;

public class GhidraDumpStringRefs extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length == 0) {
            throw new IllegalArgumentException("usage: GhidraDumpStringRefs <string> [<string> ...]");
        }

        Listing listing = currentProgram.getListing();
        for (String needle : args) {
            println("===== STRING " + needle + " =====");
            Iterator<Data> dataIterator = listing.getDefinedData(true);
            while (dataIterator.hasNext()) {
                Data data = dataIterator.next();
                Object value = data.getValue();
                if (!(value instanceof String)) {
                    continue;
                }
                String text = (String) value;
                if (!text.contains(needle)) {
                    continue;
                }
                Address address = data.getAddress();
                println("STRING_AT " + address + " " + text);
                ReferenceIterator refs = currentProgram.getReferenceManager().getReferencesTo(address);
                while (refs.hasNext()) {
                    Reference ref = refs.next();
                    if (ref.getReferenceType() == RefType.DATA || ref.getReferenceType() == RefType.READ || ref.getReferenceType() == RefType.WRITE) {
                        Address from = ref.getFromAddress();
                        Function function = currentProgram.getFunctionManager().getFunctionContaining(from);
                        if (function != null) {
                            println("REF_FROM " + from + " FUNC " + function.getEntryPoint() + " " + function.getName());
                        } else {
                            println("REF_FROM " + from);
                        }
                    }
                }
            }
        }
    }
}
