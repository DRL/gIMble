#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""usage: gIMble likelihoodold         -s FILE -g FILE -l FILE -e INT -A STR -v FILE [-k INT]
                                            [--derived_Ne FLOAT --derived_low FLOAT --derived_high FLOAT]
                                            [--migration FLOAT --migration_low FLOAT --migration_high FLOAT]
                                            [--theta FLOAT --theta_low FLOAT --theta_high FLOAT]
                                            [--time FLOAT --time_low FLOAT --time_high FLOAT]
                                            [-t INT -o STR -h]

    Options:
        -h, --help                                   show this

        -s, --sample_file <FILE>                    CSV file ("sample_id,population_id")
        -g, --genome_file <FILE>                    Genome file (as used in BedTools)
        -v, --variants_hd5 FILE                     variants HDF5 file
        -k, --kmax INT                              maximum count per mutation type per block (beyond which 
                                                        counts are combined) [default: 2]

        -l, --model FILE                            Model file
        -e, --seed INT                              Random seed for heuristic search (Nelder-mead) 
                                                        simplex algorithm to maximise model support (lnL) [default: 12345]
        -A, --ancestor_population STR               Ancestor population id
        
        --derived_Ne FLOAT                          Coalescence rate scalar for derived population (inverse of Ne)
        --derived_low FLOAT                         Lower bound derived coalescence rate scalar (will be estimated)
        --derived_high FLOAT                        Upper bound derived coalescence rate scalar (will be estimated)
        
        --migration FLOAT                           Scaled migration rate per generation (M = 4 * N_e * m) 
        --migration_low FLOAT                       Lower bound scaled migration rate (will be estimated)
        --migration_high FLOAT                      Upper bound scaled migration rate (will be estimated)
        
        --theta FLOAT                               Scaled mutation rate per block (theta = 4 * N_e * µ)
        --theta_low FLOAT                           Lower bound scaled mutation rate (will be estimated)
        --theta_high FLOAT                          Upper bound scaled mutation rate (will be estimated)
    
        --time FLOAT                                Split time (in 2*N_e*generations) 
        --time_low FLOAT                            Lower bound for split time (will be estimated)
        --time_high FLOAT                           Upper bound for split time (will be estimated)
        
        -t, --threads <INT>                         Number of threads to use [default: 1]
        -o, --prefix <STR>                          Folder/prefix for output
        
"""

from timeit import default_timer as timer
from docopt import docopt
import lib.likelihoodold

def main():
    main_time = timer()
    args = docopt(__doc__)
    print("[#] ### gIMble LIKELIHOOD ###")
    parameterObj = lib.likelihoodold.ParameterObj(args)
    entityCollection = lib.likelihoodold.task_generate_entityCollection(parameterObj)
    #print(parameterObj.boundaries)
    parameterObj.generate_mutuple_space()
    pathObj_by_path_id = lib.likelihoodold.prepare_paths(parameterObj)
    symbolic_equations_by_mutuple = lib.likelihoodold.generate_equations(pathObj_by_path_id, parameterObj)
    #print("#equations")
    #for mutuple, symbolic_equation in symbolic_equations_by_mutuple.items():
    #    print(mutuple, symbolic_equation)
    mutuple_count_matrix = parameterObj.get_mutuple_counters(entityCollection)
    #print("#matrix")
    #print(mutuple_count_matrix)
    if parameterObj.boundaries:
        lib.likelihoodold.estimate_parameters(symbolic_equations_by_mutuple, mutuple_count_matrix, parameterObj)
    else:
        lib.likelihoodold.calculate_likelihood(symbolic_equations_by_mutuple, mutuple_count_matrix, parameterObj)
        #parameterObj.write_probs(prob_by_data_string, data)
        #parameterObj.write_path_equations(pathObj_by_path_id)
    #if test == True:
    #    parameterObj.boundaries = {
    #        'Time': (sympy.Rational(str(1.3)), sympy.Rational(str(1.6))), 
    #        'theta': (sympy.Rational(str(0.1)), sympy.Rational(str(1.2)))
    #        }
    #    mutuple_count_matrix = lib.likelihoodold.calculate_pods(symbolic_equations_by_mutuple, parameterObj)
    #    lib.likelihoodold.estimate_parameters(symbolic_equations_by_mutuple, mutuple_count_matrix, parameterObj, 12345)
    #else:
    #    mutuple_count_matrix = parameterObj.get_mutuple_counters(entityCollection)
    #    if parameterObj.boundaries:
    #        lib.likelihoodold.estimate_parameters(symbolic_equations_by_mutuple, mutuple_count_matrix, parameterObj, 12345)
    #    else:
    #        lib.likelihoodold.estimate_parameters(symbolic_equations_by_mutuple, mutuple_count_matrix, parameterObj, 12345)
    #parameterObj.write_probs(prob_by_data_string, data)
    #parameterObj.write_path_equations(pathObj_by_path_id)
    print("[+] Total runtime: %s seconds" % (timer() - main_time))
        
###############################################################################

if __name__ == '__main__':
    main()