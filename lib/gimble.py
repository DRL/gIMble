import itertools
from tqdm import tqdm
from lib.functions import plot_mutuple_barchart
import allel
import numpy as np
import pandas as pd
import shutil
import zarr
import os
import string
import logging
import collections
import sys
import warnings
import pathlib
import configparser
import matplotlib.pyplot as plt
from matplotlib.path import Path
import matplotlib.patches as patches
import numpy as np
import cerberus
import lib.simulate
#import dask
import hashlib 
from timeit import default_timer as timer
import fractions
import copy
import lib.math
import tabulate
# np.set_printoptions(threshold=sys.maxsize)


'''
[Rules for better living]

- gimbleStore.data.attrs (meta): ZARR JSON encoder does not like numpy/pandas dtypes, have to be converted to python dtypes

- Coordinate systems:
    {GIMBLE}                    : 0-based       | {GIMBLE}  
    -----------------------------------------------------------
    {GENOME_FILE} : LENGTH      : 0-based   =>  | [0, l)
    {BED_FILE}    : START:END   : 0-based   =>  | [START, END)
    {VCF_FILE}    : POS         : 1-based   =>  | [POS-1)
'''

'''
[To Do]
- generalise prefligth-checks for relevant modules


[FASTA parsing]
- https://pypi.org/project/pyfasta/

    
[QC plots]
    - variants 
        - plot barcharts of HOMREF/HOMALT/HET/MISS/MULTI as proportion of total records
    - intervals
        - plot length
        - plot distance
    - mutuples
        - mutuple pcp
'''

PURPLE = '#4F3D63'

DFRM = '[──'
DFRT = '├──'
DPPM = '    '
DFRL = '└──'
DPPL = '│   '

############## Only needed once we have demand for multidimensional pairing function
# def multidimensional_box_pairing(lengths: List[int], indexes: List[int]) -> int:
#     n = len(lengths)
#     index = 0
#     dimension_product = 1
#     for dimension in reversed(range(n)):
#         index += indexes[dimension] * dimension_product
#         dimension_product *= lengths[dimension]
#     return index
# def multidimensional_szudzik_pairing(*indexes: int) -> int:
#     n = len(indexes)
#     if n == 0:
#         return 0
#     shell = max(indexes)
#     def recursive_index(dim: int):
#         slice_dims = n - dim - 1
#         subshell_count = (shell + 1) ** slice_dims - shell ** slice_dims
#         index_i = indexes[dim]
#         if index_i == shell:
#             return subshell_count * shell + multidimensional_box_pairing([shell + 1] * slice_dims, indexes[dim + 1:])
#         else:
#             return subshell_count * index_i + recursive_index(dim + 1)
#     return shell ** n + recursive_index(0)

SIGNS = {
        'T': '%s%s%s' % (u"\u251C", u"\u2500", u"\u2500"),  # '├──'
        'S': '%s%s%s' % (" ", " ", " "),                    # '    '
        'F': '%s%s%s' % (u"\u2514", u"\u2500", u"\u2500"),  # '└──'
        'W': '%s%s%s' % (u"\u250C", u"\u2500", u"\u2500"),  # '└──'
        'P': '%s%s%s' % (u"\u2502", " ", " "),              # '│   '
        'B': '%s%s%s' % (u"\u2500", u"\u2500",  u"\u2500")} # '───'

# SIGNS = {'T': '├──', 'F': '└──', 'S': '    ', 'P': '│   ', 'B': '───'}
META_TEMPLATE_BY_STAGE = {
            'seqs': {
                'vcf_f': None, 
                'sample_f': None, 
                'genome_f': None, 
                'bed_f': None,
                'seq_names': [], 
                'seq_lengths': [], 
                'seq_n50': 0,
                'samples': [], 
                'populations': [], 
                'population_ids': [], 
                'spacing' : 16,
                'sample_sets': [],
                'sample_sets_intra_A': [],
                'sample_sets_intra_B': [],
                'sample_sets_inter': [],
                'population_by_sample': {},
                'population_by_letter': {},
                'variants_counts': [], 
                'variants_idx_by_sample': {}, 
                'variants_counts_hom_ref': [],
                'variants_counts_hom_alt': [],
                'variants_counts_het': [],
                'variants_counts_missing': [],
                'intervals_count': 0, 
                'intervals_span': 0, 
                'intervals_span_sample': [],
                'intervals_idx_by_sample': {},
                'mutypes_count': 4},
            'blocks': {    
                'length': 0, 
                'span': 0, 
                'gap_run': 0,
                'max_missing': 0, 
                'max_multiallelic': 0, 
                'count_by_sample_set_idx': {},
                'count_raw_by_sample_set_idx': {},
                'maxk_by_mutype': {},
            },
            'windows' : {
                'size': 0, 
                'step': 0, 
                'count': 0,
            },
            'sims': {
            },
            'lncls': {
            },
            'grids': {
            },
            'bsfs': {
                'blocks' : {},
                'windows' : {},
            }
        }

def get_validator_error_string(validator_errors):
    out = []
    for section, errors in validator_errors.items():
        out.append("Section %s ..." % section)
        for error_dict in errors:
            for parameter, values in error_dict.items():
                out.append("[X] %s \t: %s ..." % (parameter, " ".join(values)))
    return "\n".join(out)

def calculate_bsfs_marginality(bsfs_2d, kmax_by_mutype=None):
    if not kmax_by_mutype:
        return format_percentage(0.0)
    return format_percentage(np.sum(bsfs_2d[np.any((np.array(list(kmax_by_mutype.values())) - bsfs_2d[:,1:]) < 0, axis=1), 0]) / np.sum(bsfs_2d[:,0]))

def DOL_to_LOD(DOL):
    """
    converts dict of lists to list of dicts
    """
    reshape_DOL = list(zip(*DOL.values()))
    return [{k:v for k,v in zip(DOL.keys(),sublist)} for sublist in reshape_DOL]  

def LOD_to_DOL(LOD):
    """
    converts list of dicts to dict of lists
    """
    reshape_LOD = list(zip(*(d.values() for d in LOD)))
    return {k:np.array(v,dtype=np.float64) for k,v in zip(LOD[0].keys(),reshape_LOD)}

class ReportObj(object):
    '''Report class for making reports'''

    def __init__(self, width=80):
        '''
        Parameters 
        ----------
        width : int, width of report in characters, default=80
        
        Returns
        -------
        out : instance
        '''
        self.width = width
        self.out = []
    
    def add_line(self, prefix="[+]", branch="", left='', center='', right='', fill=' '):
        '''
        Writes line to Report Object
        Lines is composed of {prefix} {branch} {left} {fill} {center} {fill} {right}
        SIGNS = {'T': '├──', 'F': '└──', 'S': '    ', 'P': '│   ', 'B': '───'}
        '''
        start = "%s %s" % (prefix, "".join([SIGNS[b] for b in branch])) if branch else prefix
        w = self.width - len(left) - len(right) - len(start) 
        self.out.append("%s %s %s %s" % (start, left, (" %s " % center).center(w, fill) if center else fill * w, right))

    def __add__(self, other):
        if isinstance(other, ReportObj):
            if self.width == other.width:
                result = ReportObj(width=self.width)
                result.out = self.out + other.out
                return result
            else:
                raise ValueError("addition only supported for ReportObj of equal width")    
        else:
            raise ValueError("addition only supported for ReportObj's")

    def __radd__(self, other):
        return self.__add__(other)

    def __repr__(self):
        return "\n".join(self.out)
    
def get_config_schema(module):
    schema = {
        'gimble': {
            'type': 'dict',
            'schema': {
                'version': {'required': True, 'empty':False, 'type': 'string'},
                'precision': {'required': True, 'empty':False, 'type': 'integer', 'coerce': int},
                'random_seed': {'required': True, 'empty':False, 'type': 'integer', 'coerce': int}}},
        'populations': {
            'required': True, 'type': 'dict', 
            'schema': {
                'A': {'required':True, 'empty':True, 'type':'string'},
                'B': {'required':True, 'empty':True, 'type':'string'},
                'reference_pop': {'required':True, 'empty':False, 'isPop':True},
                'sync_pop_sizes': {'required':False, 'empty':True, 'isPopSync':True},
                }},
        'k_max': {
            'type': 'dict', 
            'valuesrules': {'required': True, 'empty':False, 'type': 'integer', 'min': 1, 'coerce':int}},
        'mu': {
            'type':'dict', 
            'schema':{
                'mu': {'required': False, 'empty':True, 'type': 'float', 'coerce':float},
                'blocklength': {'required': False, 'empty':True,'notNoneInt':True, 'coerce':'int_or_empty'}
                }},
        'parameters': {
            'type': 'dict', 'required':True, 'empty':False, 
            'valuesrules': {'coerce':'float_or_list', 'notNone':True}
        },
        'simulations':{
            'required':False, 'empty':True}
        }
    if module == 'simulate':
        schema['simulations'] = {
        'type': 'dict',
        'required':True,
        'schema': {
            'ploidy': {'required':True,'empty':False, 'required':True, 'type':'integer', 'min':1, 'coerce':int},
            'blocks': {'required':True, 'empty':False, 'type': 'integer', 'min':1, 'coerce':int},
            'chunks': {'required':True, 'empty':False, 'type': 'integer', 'min':1, 'coerce':int},
            'replicates': {'required':True,'empty': False, 'type': 'integer', 'min':1, 'coerce':int},
            'sample_size_A': {'required':True,'empty':False, 'type': 'integer', 'min':1, 'coerce':int},
            'sample_size_B': {'required':True,'empty':False, 'type': 'integer', 'min':1, 'coerce':int},
            'recombination_rate': {'empty': True, 'notNoneFloat':True, 'coerce':'float_or_empty', 'min':0.0},
            'recombination_map': {'empty': True, 'type': 'string', 'isPath':True, 'dependencies':['number_bins', 'cutoff', 'scale']},
            'number_bins': {'empty': True, 'notNoneInt': True, 'coerce':'int_or_empty', 'min':1},
            'cutoff': {'empty': True, 'notNoneFloat': True, 'coerce':'float_or_empty', 'min':0},
            'scale': {'empty':True, 'type':'string', 'allowed':['lin', 'log']}
        }}
        schema['mu'] = {
            'type':'dict', 
            'schema':{
                'mu': {'required': True, 'empty':False, 'type': 'float', 'coerce':float},
                'blocklength': {'required': False, 'empty':True, 'min':1, 'type': 'integer', 'coerce':int}
                }}
    return schema

def recursive_get_size(path):
    """Gets size in bytes of the given path, recursing into directories."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    if not os.path.isdir(path):
        return 0
    return sum(
        recursive_get_size(os.path.join(path, name))
        for name in os.listdir(path))

def parse_csv(csv_f='', dtype=[], usecols=[], sep=',', header=None):
    '''dtypes := "object", "int64", "float64", "bool", "datetime64", "timedelta", "category"'''
    df = pd.read_csv(csv_f, sep=sep, usecols=usecols, names=list(dtype.keys()), header=header, dtype=dtype)
    if df.isnull().values.any():
        sys.exit("[X] Bad file format %r." % csv_f)
    return df

def format_bases(bases):
    if bases in set(['-', 'N/A']):
        return bases
    return "%s b" % format(bases, ',d')

def format_percentage(fraction, precision=2):
    if fraction in set(['-', 'N/A']):
        return fraction
    return "{:.{}%}".format(fraction, precision)

def format_proportion(fraction, precision=2):
    if fraction in set(['-', 'N/A']):
        return fraction
    return "{:.{}f}".format(fraction, precision)

def format_count(count):
    if count in set(['-', 'N/A']):
        return count
    return "%s" % str(format(count, ',d'))

    

def format_bytes(size, precision=1):
    power = 2**10
    n = 0
    power_labels = {0 : ' B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power:
        size /= power
        n += 1
    return "{:.{}f} {}".format(size, precision, power_labels[n])

def get_n50_from_lengths(lengths):
    length_sorted = sorted(lengths, reverse=True)
    cum_sum = np.cumsum(length_sorted)
    half = int(sum(lengths) / 2)
    cum_sum_2 = min(cum_sum[cum_sum >= half])
    n50_idx = np.where(cum_sum == cum_sum_2)
    return length_sorted[int(n50_idx[0][0])]

def pop_metrics_from_bsfs(bsfs, block_length=None, window_size=None):
    '''only works for mutypes=4'''
    if not bsfs.ndim == 2:
        bsfs = bsfs_to_2d(bsfs)
    mutype_array = np.vstack([np.bincount(bsfs[:, 0], weights=bsfs[:, 1] * bsfs[:, (2 + m_idx)]) for m_idx in range(4)]).T 
    heterozygosity_A = (mutype_array[:,1] + mutype_array[:,2]) / (block_length * window_size)
    heterozygosity_B = (mutype_array[:,0] + mutype_array[:,2]) / (block_length * window_size)
    d_xy = ((mutype_array[:,1] + mutype_array[:,0] + mutype_array[:,2]) / 2.0 + mutype_array[:,3]) / (block_length * window_size)
    mean_pi = (heterozygosity_A + heterozygosity_B) / 2.0
    f_st = np.full(mutype_array.shape[0], np.nan)
    np.true_divide((d_xy - mean_pi), (d_xy + mean_pi), out=f_st, where=(d_xy + mean_pi) > 0)
    pop_metrics = np.vstack([heterozygosity_A, heterozygosity_B, d_xy, f_st])
    return pop_metrics

def check_unique_pos(pos_array):
    unique_pos, counts_pos = np.unique(pos_array, return_counts=True)
    duplicates = unique_pos[counts_pos > 1]
    if duplicates.any():
        print("\n[-] %s VCF records with non-unique positions found. Rescuing records by shifting position... (abort if this is not desired)" % (len(duplicates)))
        pos_array = fix_pos_array(pos_array)
    return pos_array

def fix_pos_array(pos_array):
    '''
    De-duplicates array by shifting values forward until there aren't any collisions
    '''
    # get boolean array for first and subsequent duplicates (True) (required sorted) 
    idxs = np.insert((np.diff(pos_array)==0).astype(np.bool), 0, False)
    if np.any(idxs): 
        # if there are duplicates, get new values by incrementing by one
        new_values = pos_array[idxs]+1
        # get non-duplicate values
        uniq_values = pos_array[~idxs]
        # insert new_values in non-duplicated values (required sorted)
        new_idxs = np.searchsorted(uniq_values, new_values)
        # recursive call
        return fix_pos_array(np.sort(np.insert(uniq_values, new_idxs, new_values)))
    # if there are no duplicated values
    return pos_array

def szudzik_pairing(folded_minor_allele_counts):
    # adapted from: https://drhagen.com/blog/superior-pairing-function/
    return np.where(
            (folded_minor_allele_counts[:,0] >= folded_minor_allele_counts[:,1]),
            np.square(folded_minor_allele_counts[:,0]) + folded_minor_allele_counts[:,0] + folded_minor_allele_counts[:,1],
            folded_minor_allele_counts[:,0] + np.square(folded_minor_allele_counts[:,1])
            )
    #if isinstance(folded_minor_allele_counts, np.ndarray):
    #    # assumes folded_minor_allele_counts is array with shape (n,2)
    #    return np.where(
    #        (folded_minor_allele_counts[:,0] >= folded_minor_allele_counts[:,1]),
    #        np.square(folded_minor_allele_counts[:,0]) + folded_minor_allele_counts[:,0] + folded_minor_allele_counts[:,1],
    #        folded_minor_allele_counts[:,0] + np.square(folded_minor_allele_counts[:,1])
    #        )
    #elif isinstance(folded_minor_allele_counts, tuple):
    #    # assumes folded_minor_allele_counts is tuple of size 2
    #    a, b = folded_minor_allele_counts
    #    if a >= b:
    #        return (a**2) + a + b 
    #    return a + (b**2)
    #else:
    #    pass

def get_coverage_counts(coverages, idxs, num_blocks):
    # not used at the moment
    num_sample_sets = idxs[-1] + 1 # this is correct, don't worry about it ...
    temp = coverages + (num_sample_sets * np.arange(coverages.shape[0]))[:, None]
    blocks_per_sample_set = np.bincount(temp.ravel(), minlength=(num_sample_sets * coverages.shape[0])).reshape(-1, num_sample_sets)
    # remove columns/sample_sets that only contain zeroes and normalise
    return blocks_per_sample_set[:, ~(blocks_per_sample_set == 0).all(0)] / (num_blocks / len(idxs)) / num_blocks 

def block_sites_to_variation_arrays(block_sites, cols=np.array([1,2,3]), max_type_count=7):
    temp_sites = block_sites + (max_type_count * np.arange(block_sites.shape[0], dtype=np.int64).reshape(block_sites.shape[0], 1))
    multiallelic, missing, monomorphic, variation = np.hsplit(np.bincount(temp_sites.ravel(), minlength=(block_sites.shape[0] * max_type_count)).reshape(-1, max_type_count), cols)
    return (multiallelic, missing, monomorphic, variation)
    # return np.hsplit(np.bincount(temp_sites.ravel(), minlength=(block_sites.shape[0] * max_type_count)).reshape(-1, max_type_count), cols)

# def calculate_popgen_from_array(mutype_array, sites):
#     # print('# Mutypes: 0=MULTI, 1=MISS, 2=MONO, 3=HetB, 4=HetA, 5=HetAB, 6=Fixed')
#     pi_1 = float("%.8f" % np.divide(np.sum(mutype_array[:, 1]) + np.sum(mutype_array[:, 2]), sites)) # average heterozygosity
#     pi_2 = float("%.8f" % np.divide(np.sum(mutype_array[:, 0]) + np.sum(mutype_array[:, 2]), sites)) # average heterozygosity
#     d_xy = float("%.8f" % np.divide(np.divide(np.sum(mutype_array[:, 0]) + np.sum(mutype_array[:, 1]) + np.sum(mutype_array[:, 2]), 2.0) + np.sum(mutype_array[:, 3]), sites))
#     mean_pi = (pi_1 + pi_2) / 2.0
#     total_pi = (d_xy + mean_pi) / 2.0 # special case of pairwise Fst
#     f_st = np.nan
#     if (total_pi):
#         f_st = float("%.8f" % ((total_pi - mean_pi) / total_pi)) # special case of pairwise Fst
#     fgv = len(mutype_array[(mutype_array[:, 2] > 0) & (mutype_array[:, 3] > 0)])
#     return (pi_1, pi_2, d_xy, f_st, fgv)

def genotype_to_mutype_array(sa_genotype_array, idx_block_sites_in_pos, block_sites, debug=True):
    '''
    - possible errors:
        - if whole sequence has only missing GTs in genotypes for a sample set, then np_allele_count_array will be empty ... (should never happen)
    '''
    np_genotype_array = np.array(sa_genotype_array)
    #print('np_genotype_array', np_genotype_array.shape, np_genotype_array)
    np_allele_count_array = np.ma.masked_equal(sa_genotype_array.count_alleles(), 0, copy=True) 
    #print('np_allele_count_array', np_allele_count_array.shape, np_allele_count_array)
    #print('np.any(np_allele_count_array)', np.any(np_allele_count_array))
    allele_map = np.ones((np_allele_count_array.shape), dtype=np.int64) * np.arange(np_allele_count_array.shape[-1], dtype=np.int64)
    #print('allele_map', allele_map.shape, allele_map)
    #print('np.any(allele_map)', np.any(allele_map))
    if np.any(np_allele_count_array) and np.any(allele_map):
        idx_max_global_allele_count = np.nanargmax(np_allele_count_array, axis=1)
        idx_min_global_allele_count = np.nanargmin(np_allele_count_array, axis=1)
        has_major_allele = (idx_max_global_allele_count != idx_min_global_allele_count)
        idx_min_prime_allele = np.amin(np_genotype_array[:,0], axis=1)
        idx_min_global_allele = np.amin(np.amin(np_genotype_array, axis=1), axis=1)
        idx_max_global_allele = np.amax(np.amax(np_genotype_array, axis=1), axis=1)
        idx_major_allele = np.where(
            has_major_allele, 
            idx_max_global_allele_count, 
            idx_min_prime_allele)
        idx_minor_allele = np.where(
            has_major_allele, 
            idx_min_global_allele_count, 
            np.where((
                idx_min_global_allele == idx_min_prime_allele),
                np.max((idx_min_global_allele, idx_max_global_allele), axis=0), 
                np.min((idx_min_global_allele, idx_max_global_allele), axis=0)))
        # for each genotype (np.arange(allele_map.shape[0])), set minor allele to 1 (1st do minor, so that overwritten if monomorphic)
        allele_map[np.arange(allele_map.shape[0]), idx_minor_allele] = 1 
        # for each genotype (np.arange(allele_map.shape[0])), set major allele to 0
        allele_map[np.arange(allele_map.shape[0]), idx_major_allele] = 0
    folded_minor_allele_counts = sa_genotype_array.map_alleles(allele_map).to_n_alt(fill=-1)
    #print(folded_minor_allele_counts)
    folded_minor_allele_counts[np.any(sa_genotype_array.is_missing(), axis=1)] = np.ones(2) * -1        # -1, -1 for missing => -1
    folded_minor_allele_counts[(np_allele_count_array.count(axis=1) > 2)] = np.ones(2) * (-1, -2)       # -1, -2 for multiallelic => -2

    block_sites[idx_block_sites_in_pos] = szudzik_pairing(folded_minor_allele_counts) + 2               # add 2 so that not negative for bincount
    block_sites[~idx_block_sites_in_pos] = 2                                                            # monomorphic = 2 (0 = multiallelic, 1 = missing)
    if debug == True:
        print("# block_sites as mutype array", block_sites)
    if debug == True:
        block_sites_pos = block_sites.flatten()
        pos_df = pd.DataFrame(block_sites_pos[idx_block_sites_in_pos.flatten()], dtype='int64', columns=['pos'])
        genotypes_df = pd.DataFrame(np_genotype_array.reshape(np_genotype_array.shape[0], 4), dtype='i4', columns=['a1', 'a2', 'b1', 'b2'])        
        block_sites_df = pos_df.join(genotypes_df)
        folded_minor_allele_count_df = pd.DataFrame(folded_minor_allele_counts, dtype='int8', columns=['fmAC_a', 'fmAC_b'])
        block_sites_df = block_sites_df.join(folded_minor_allele_count_df)
        variants = pd.DataFrame(block_sites[idx_block_sites_in_pos], dtype='int', columns=['SVar'])
        block_sites_df = block_sites_df.join(variants)
        print('# Mutypes: 0=MULTI, 1=MISS, 2=MONO, 3=HetB, 4=HetA, 5=HetAB, 6=Fixed')
        print(block_sites_df)
    return block_sites

def _harmonic(a, b):
    if b-a == 1:
        return fractions.Fraction(1,a)
    m = (a+b)//2
    return _harmonic(a,m) + _harmonic(m,b)

def harmonic(n):
    '''https://fredrik-j.blogspot.com/2009/02/how-not-to-compute-harmonic-numbers.html'''
    return _harmonic(1,n+1)

def create_ranges(aranges):
    # does the transformation form 0-based (BED) to 1-based (VCF) coordinate system 
    # https://stackoverflow.com/a/47126435
    l = (aranges[:, 1] - aranges[:, 0])
    clens = l.cumsum()
    if np.any(clens):
        ids = np.ones(clens[-1], dtype=np.int64)
        ids[0] = aranges[0, 0]
        ids[clens[:-1]] = aranges[1:, 0] - aranges[:-1, 1] + 1
        return ids.cumsum()
    return None

def cut_windows(mutype_array, idxs, start_array, end_array, num_blocks=10, num_steps=3):
    coordinate_sorted_idx = np.argsort(end_array)
    mutype_array_sorted = mutype_array.take(coordinate_sorted_idx, axis=0)
    window_idxs = np.arange(mutype_array_sorted.shape[0] - num_blocks + 1)[::num_steps, None] + np.arange(num_blocks)
    window_mutypes = mutype_array_sorted.take(window_idxs, axis=0)
    block_starts = start_array.take(coordinate_sorted_idx, axis=0).take(window_idxs, axis=0)
    window_starts = np.min(start_array.take(coordinate_sorted_idx, axis=0).take(window_idxs, axis=0), axis=1).T
    block_ends = end_array.take(coordinate_sorted_idx, axis=0).take(window_idxs, axis=0)#[:,-1]
    window_ends = np.max(end_array.take(coordinate_sorted_idx, axis=0).take(window_idxs, axis=0), axis=1).T
    window_midpoints = (block_starts / 2) + (block_ends / 2)
    window_pos_mean = np.mean(window_midpoints, axis=1).T
    window_pos_median = np.median(window_midpoints, axis=1).T
    return window_mutypes, window_starts, window_ends, window_pos_mean, window_pos_median

def cut_blocks(interval_starts, interval_ends, block_length, block_span, block_gap_run):
    sites = create_ranges(np.array((interval_starts, interval_ends), dtype=np.int64).T)
    if sites is None: 
        return None
    block_sites = np.concatenate([
        x[:block_length * (x.shape[0] // block_length)].reshape(-1, block_length) 
            for x in np.split(sites, np.where(np.diff(sites) > block_gap_run)[0] + 1)
        ]) 
    block_span_valid_mask = (((block_sites[:, -1] - block_sites[:, 0]) + 1) <= block_span)
    return block_sites[block_span_valid_mask]

def bsfs_to_2d(bsfs):
    """Converts 4D bsfs to 2D array with counts, mutuples.
       Converts 5D bsfs to 2D array with window_idx, counts, mutuples.

    Parameters 
    ----------
    bsfs : ndarray, int, ndim (4 or 5)  
    
    Returns
    -------
    out : ndarray, int, ndim (2)
    """
    if not np.any(bsfs):
        return None
    non_zero_idxs = np.nonzero(bsfs)
    if bsfs.ndim == 4: # blocks
        return np.concatenate([bsfs[non_zero_idxs].reshape(non_zero_idxs[0].shape[0], 1), np.array(non_zero_idxs).T], axis=1)
    elif bsfs.ndim == 5: # windows
        non_zero_idxs_array = np.array(non_zero_idxs).T
        first = non_zero_idxs_array[:,0].reshape(non_zero_idxs[0].shape[0], 1)
        second = bsfs[non_zero_idxs].reshape(non_zero_idxs[0].shape[0], 1)
        third = non_zero_idxs_array[:,1:]
        return np.concatenate([first, second, third], axis=1)
    else:
        raise ValueError('bsfs_to_2d: bsfs.ndim must be 4 (blocks) or 5 (windows)')

def sum_wbsfs(bsfs_windows):
    assert bsfs_windows.ndim == 5, "only works for bsfs_windows.ndim = 5"
    return bsfs_windows.sum(axis=0)

def bsfs_to_counter(bsfs):
    """Returns (dict of) collections.Counter based on bsfs_array of 4 or 5 dimensions.

    Parameters 
    ----------
    bsfs : ndarray, int, ndim (4) or (5)  
    
    Returns
    -------
    out : (dict of) collections.Counter

    """
    non_zero_idxs = np.nonzero(bsfs)
    if bsfs.ndim == 5:
        out = collections.defaultdict(collections.Counter())
        for idx, count in zip(tuple(np.array(non_zero_idxs).T), bsfs[non_zero_idxs]):
            window_idx, mutuple = idx[0], idx[1:]
            out[window_idx][mutuple] = count
    elif bsfs.ndim == 4:
        out = collections.defaultdict(lambda: collections.Counter())
        for mutuple, count in zip(tuple(np.array(non_zero_idxs).T), bsfs[non_zero_idxs]):
            out[window_idx][mutuple] = count
    else:
        raise ValueError('bsfs must have 4 or 5 dimensions')
    return out

def ordered_intersect(a=[], b=[], order='a'):
    A, B = a, b
    if order == 'b' :
        B, A = a, b
    return [_a for _a in A if _a in set(B)]

def get_hash_from_dict(d):
    '''returns md5sum hash of str(dict)'''
    if isinstance(d, dict):
        return hashlib.md5(str(d).encode()).hexdigest()
    raise ValueError('must be a dict')

def grid_meta_dict_to_value_arrays_by_parameter(grid_meta_dict):
    #see function lib.gimble.LOD_to_DOL() -> should be redundant
    _values_by_parameter = collections.defaultdict(list)
    for grid_idx, grid_dict in grid_meta_dict.items():
        for key, value in grid_dict.items():
            _values_by_parameter[key].append(value)
    values_by_parameter = {}
    for key, values in _values_by_parameter.items():
        values_by_parameter[key] = np.array(values)
    return values_by_parameter

class CustomNormalizer(cerberus.Validator):
    def __init__(self, *args, **kwargs):
        super(CustomNormalizer, self).__init__(*args, **kwargs)
        self.valid_pop_ids = kwargs['valid_pop_ids']
        self.valid_sync_pops = kwargs['valid_sync_pops']
        
    def _normalize_coerce_float_or_list(self, value):
        try:
            return float(value)
        except:
            values = value.strip('()[]').split(",")
            if len(values) == 2:
                try:
                    return [float(v) for v in values]
                except ValueError:
                    return None
            elif len(values) == 4:
                valid_scales = set(['lin', 'log'])
                if not values[-1].strip(' ') in valid_scales:
                    return None
                try:
                    return [float(v) for v in values[:-2]] + [int(values[-2]), values[-1].strip(' ')]
                except ValueError:
                    return None
            else:
                return None

    def _normalize_coerce_float_or_empty(self, value):
        try:
            return float(value)
        except:
            if value.strip(' ') =='':
                return ''
            else:
                return None

    def _normalize_coerce_int_or_empty(self, value):
        try:
            return int(value)
        except:
            if value.strip(' ')=='':
                return ''
            else:
                return None

    def _validate_notNoneInt(self, notNoneNumeric, field, value):
        """
        {'type':'boolean'}
        """
        if value == None and notNoneNumeric:
            self._error(field, "Must be an int value or empty")
    
    def _validate_notNoneFloat(self, notNoneNumeric, field, value):
        """
        {'type':'boolean'}
        """
        if value == None and notNoneNumeric:
            self._error(field, "Must be a float or empty")

    def _validate_notNone(self, notNone, field, value):
        """
        {'type':'boolean'}
        """
        if not value and notNone:
            self._error(field, "Must be FLOAT, or (MIN, MAX), or (MIN, MAX, STEPS, LIN|LOG).")

    def _validate_isPop(self, isPop, field, value):
        """
        {'type':'boolean'}
        """
        if value.strip(" ") not in self.valid_pop_ids:
            self._error(field, "Must be either A, B or A_B")

    def _validate_isPopSync(self, isPopSync, field, value):
        """
        {'type':'boolean'}
        """
        if value.strip(" ") !='':
            if value.strip(" ") not in self.valid_sync_pops:
                self._error(field, "Must be either A,A_B, A,B or A_B,B")

    def _validate_isPath(self, isPath, field, value):
        """
        {'type':'boolean'}
        """
        if value.strip(" ") != '' and not os.path.isfile(value):
            self._error(field, 'Must be a valid path to the recombination map.')

#def get_unique_hash(parameterObj, purpose='makegrid'):
#    if purpose == 'makegrid':
#        print(parameterObj)

class ParameterObj(object):
    '''Superclass ParameterObj'''
    def __init__(self, params):
        self._PATH = params['path']
        self._VERSION = params['version']
        self._MODULE = params['module']
        self._CWD = params['cwd']
        self.config = None

    def __repr__(self):
        return("[+] VER := %s\n[+] CWD := %s\n[+] CMD := %s\n" % (
            self._VERSION, 
            self._CWD, 
            self._get_cmd()
            ))

    def _dict_product(self, parameter_dict):
        cartesian_product = itertools.product(*parameter_dict.values())
        rearranged_product = list(zip(*cartesian_product))
        return {k:np.array(v,dtype=np.float64) for k,v in zip(parameter_dict.keys(), rearranged_product)}

    def _dict_zip(self, pdict):
        '''DRL: if this is only used once, no need for being separate function'''
        return [dict(zip(pdict, x)) for x in zip(*pdict.values())]

    def _get_blocks_length(self):
        blocks_length_zarr = None
        blocks_length_ini = self._get_int(self.config['mu']['blocklength'], ret_none=True)
        if self.zstore:
            gimbleStore = lib.gimble.Store(path=self.zstore, create=False, overwrite=False)
            meta_blocks = gimbleStore._get_meta('blocks')
            if isinstance(meta_blocks, dict):
                blocks_length_zarr = meta_blocks.get('length', None)
        if blocks_length_zarr and blocks_length_ini:
            if blocks_length_zarr != blocks_length_ini:
                print("[-] Block length in INI and gimbleStore differ. Using block length from INI : %s b" % blocks_length_ini)
                return blocks_length_ini
            return blocks_length_zarr
        if blocks_length_zarr:
            return blocks_length_zarr
        if blocks_length_ini:
            return blocks_length_ini
        else:
            sys.exit("[X] Blocklength needs to be specified in ini file.")

    def _get_int(self, string, ret_none=False):
        try:
            return int(string)
        except TypeError:
            if not ret_none:
                sys.exit("[X] %r can't be converted to interger." % string)
            return None
        except ValueError:
            if not ret_none:
                sys.exit("[X] %r can't be converted to interger." % string)
            return None

    def _get_float(self, string, ret_none=False):
        try:
            return float(string)
        except TypeError:
            if not ret_none:
                sys.exit("[X] %r can't be converted to float." % string)
            return None

    def _get_cmd(self):
        return "%s/gimble %s %s" % (
            self._PATH, 
            self._MODULE, 
            "".join(["--%s " % " ".join((k, str(v))) for k,v in self.__dict__.items() if not k.startswith("_")]))

    def _get_fixed_params(self, subgroup=None, as_dict=False):
        if not as_dict:
            fixed_params =  [param for param,value in self.config['parameters'].items() if isinstance(value,float) or isinstance(value,int)]
            if subgroup:
                fixed_params = [param for param in fixed_params if param.startswith(subgroup)]
            return fixed_params
        else:
            fixedParams = {k:next(iter(v)) for k, v in self.parameter_combinations.items() if len(set(v))==1}
            fixedParams['mu'] = self.config['mu']['mu']
            if subgroup:
                fixedParams = {k:v for k,v in fixedParams.items() if k.startswith(subgroup)}
            return fixedParams

    def _get_path(self, infile, path=False):
        if infile is None:
            return None
        _path = pathlib.Path(infile).resolve()
        if not _path.exists():
            sys.exit("[X] File not found: %r" % str(infile))
        if path:
            return _path
        return str(_path)

    def _get_prefix(self, prefix):
        if prefix is None:
            return None
        path = pathlib.Path(prefix).resolve()
        new_path = pathlib.Path(prefix+'.z').resolve()
        if new_path.exists():
            sys.exit("[X] zstore already exists. Specify using -z or provide new prefix.")
        parent_path = pathlib.Path(prefix).resolve().parent
        if not parent_path.exists():
            sys.exit("[X] Path does not exist: %r" % str(parent_path))
        return str(path)

    def _get_pops_to_sync(self, config=None, valid_sync_pops=None):
        reference, to_be_synced = None, None
        if config:
            syncing = config['populations']['sync_pop_sizes']
            reference_pop = config['populations']['reference_pop']
        else:
            syncing =  self.config['populations']['sync_pop_sizes']
        if syncing:
            if len(syncing)>0:
                syncing = syncing.split(',')
                reference = syncing[0]
                to_be_synced = syncing[1:]
                if self.config['populations']['reference_pop'] in to_be_synced:
                    sys.exit(f"[X] Set reference pop to {reference}.")
                reference_size = config['parameters'][f'Ne_{reference}']
                tBS_sizes = [config['parameters'][f'Ne_{pop}'] for pop in to_be_synced]
                reference_size = [s for s in tBS_sizes if s!=None and s!=reference_size and s!='']
                if len(reference_size)>0:
                    sys.exit(f"[X] Syncing pop sizes: set no value or the same value for Ne_{', Ne_'.join(to_be_synced)} as for Ne_{reference}")         
                if self._MODULE == 'optimize':
                    fixed_Nes = self._get_fixed_params(subgroup='Ne')
                    if len(fixed_Nes)>0:
                        if not f"Ne_{reference_pop}" in fixed_Nes:
                            sys.exit("[X] No. No. No. It would make much more sense to set a population with a fixed size as reference.")

        return (reference, to_be_synced)

    def _get_unique_hash_from_dict(self, d):
        '''active'''
        return hashlib.md5(str(d).encode()).hexdigest()

    def _get_unique_hash(self, return_dict=False, module=None):
        '''passive'''
        module = module if module else self._MODULE
        to_hash = copy.deepcopy(self.config)
        if module in set(['makegrid','gridsearch', 'query']):
            del to_hash['simulations']
            if 'kmax_by_mutype' in to_hash:
                del to_hash['--kmax_by_mutype']
            if 'recombination' in to_hash['parameters']:
                del to_hash['parameters']['recombination']
        elif module == 'simulate':
            pass
        else:
            ValueError("Not implemented yet.")
        del to_hash['population_by_letter']
        for pop_name in ['A', 'B']:
            del to_hash['populations'][pop_name]
        del to_hash['gimble']
        if return_dict:
            return (hashlib.md5(str(to_hash).encode()).hexdigest(), to_hash)
        return hashlib.md5(str(to_hash).encode()).hexdigest()

    def _expand_params(self, remove=None):
        if len(self.config['parameters'])>0:
            parameter_combinations = collections.defaultdict(list)
            if remove:
                for key in remove:
                    del self.config['parameters'][f'Ne_{key}']
            for key, value in self.config['parameters'].items():
                if isinstance(value, float) or isinstance(value, int):
                    parameter_combinations[key]=np.array([value,], dtype=np.float64)
                elif key=='recombination':
                    pass
                else:
                    if len(value) == 4:
                        if self._MODULE == 'optimize':
                            sys.exit(f"[X] {self._MODULE} requires a single point or boundary for all parameters.")
                        minv, maxv, n, scale = value
                        sim_range = self._expand_params_scale(scale, minv, maxv, n)
                        parameter_combinations[key] = sim_range
                    elif len(value) <= 2:
                        parameter_combinations[key] = np.unique(value)
                    else:
                        raise ValueError("Uncaught error in config file configuration.")
            return parameter_combinations
        else:
            raise ValueError("config parameters does not contain any parameters.")

    def _expand_params_scale(self, scale, minv, maxv, n):
        if scale.startswith('lin'):
            return np.linspace(minv, maxv, num=n, endpoint=True, dtype=np.float64)
        elif scale.startswith('log'):
            return np.logspace(minv, maxv, num=n, endpoint=True, dtype=np.float64)
        else:
            sys.exit("scale in config parameters should either be lin or log")

    def _make_parameter_combinations(self, sync_reference=None, sync_target=None):
        parameter_combinations = self._expand_params(remove=sync_target)
        if self._MODULE == 'optimize':
            return parameter_combinations
        if self._MODULE == 'simulate':
            rec = self._set_recombination_rate()
            if rec != None:
                parameter_combinations['recombination'] = rec
        parameter_combinations =  self._dict_product(parameter_combinations)
        if sync_reference and sync_target:
            for pop in sync_target:
                parameter_combinations[f'Ne_{pop}'] = parameter_combinations[f'Ne_{sync_reference}']
        return parameter_combinations

    def _parse_config(self, config_file):
        '''validates types in INI config, returns dict with keys, values as sections/params/etc
        - does not deal with missing/incompatible values (should be dealt with in ParameterObj subclasses)
            https://docs.python-cerberus.org/en/stable/usage.html

        DRL: could the whole 'possible_values' stuff be removed by adding population_ids as field to the INI filed coming from model?
            (population_ids = A, B, A_B)

        '''
        #validating config
        if config_file is None:
            return None
        raw_config = configparser.ConfigParser(inline_comment_prefixes='#', allow_no_value=True)
        raw_config.optionxform=str # otherwise keys are lowercase
        raw_config.read(config_file)
        config = {s: dict(raw_config.items(s)) for s in raw_config.sections()}
        possible_values_config = configparser.ConfigParser(allow_no_value=True, strict=False, comment_prefixes=None)
        possible_values_config.optionxform=str # otherwise keys are lowercase
        possible_values_config.read(config_file)
        possible_values_dict = {s: dict(possible_values_config.items(s)) for s in possible_values_config.sections()}
        # possible_values_dict is only needed for valid_pop_ids and valid_sync_pops
        valid_pop_ids = [population.strip(" ") for population in possible_values_dict["populations"]["# possible values reference_pop"].split("|")]
        sample_pop_ids = [population for population in valid_pop_ids if "_" not in population]
        schema = get_config_schema(self._MODULE)
        sync_pops = config["populations"]["sync_pop_sizes"].strip(" ")
        valid_sync_pops = [population.strip(" ") for population in possible_values_dict["populations"]["# possible values sync_pop_sizes"].split("|")]
        
        validator = CustomNormalizer(schema, valid_pop_ids=valid_pop_ids, valid_sync_pops=valid_sync_pops)
        validator.validate(config)
        if not validator.validate(config):
            validator_error_string = get_validator_error_string(validator.errors)
            sys.exit("[X] INI Config file format error(s) ...\n%s" % validator_error_string)
        self.config = validator.normalized(config)

        self.config['populations']['sample_pop_ids'] = sample_pop_ids
        self.config['population_by_letter'] = {pop:config['populations'][pop] for pop in sample_pop_ids}
        self.config['mu']['blocklength'] = self._get_blocks_length()
        self.reference, self.toBeSynced = self._get_pops_to_sync(config, valid_sync_pops)
        self.parameter_combinations = self._make_parameter_combinations(sync_reference=self.reference, sync_target=self.toBeSynced)

class Store(object):
    def __init__(self, prefix=None, path=None, create=False, overwrite=False):
        self.prefix = prefix if not prefix is None else str(pathlib.Path(path).resolve().stem)
        self.path = path if not path is None else "%s.z" % prefix
        self.data = self._init_store(create, overwrite)
        if create:
            self._init_meta(overwrite=overwrite)

    def tree(self):
        print(self.data.tree())
    
    def log_stage(self, parameterObj):
        '''only remembers last command per module'''
        self.data.attrs[parameterObj._MODULE] = parameterObj._get_cmd()
    
    def get_stage(self, stage):
        return self.data.attrs[stage]

    def has_stage(self, stage):
        return stage in self.data.attrs

    def setup_sim(self, parameterObj):
        print("[#] Preparing store...")
        self._init_meta(overwrite=True)

    def setup_debug(self, parameterObj):
        print("[#] Preparing store...")
        self._init_meta(overwrite=True)
        self._set_bsfs(parameterObj)
        # inference can be done on ETP-counts

    def setup_seq(self, parameterObj):
        print("[#] Preparing store...")
        self._init_meta(overwrite=True)
        print("[#] Processing GENOME_FILE %r..." % parameterObj.genome_f)
        self._set_sequences(parameterObj)
        print("[#] Processing SAMPLE_FILE %r..." % parameterObj.sample_f)
        self._set_samples(parameterObj)
        print("[#] Processing VCF_FILE %r..." % parameterObj.vcf_f)
        self._set_variants(parameterObj)
        print("[#] Processing BED_FILE %r..." % parameterObj.bed_f)
        self._set_intervals(parameterObj)
        self.log_stage(parameterObj)

    def blocks(self, parameterObj):
        print("[#] Preflight...")
        self._preflight_blocks(parameterObj)
        print("[#] Making blocks...")
        #self._make_blocks_threaded(parameterObj)
        self._make_blocks(parameterObj)
        #self.plot_bsfs_pcp(sample_set='X')
        #self._calculate_block_pop_gen_metrics()
        #self._plot_blocks(parameterObj)
        self.log_stage(parameterObj)

    def windows(self, parameterObj):
        print("[#] Preflight...")
        self._preflight_windows(parameterObj)
        print("[#] Making windows...")
        self._make_windows(parameterObj, sample_sets='X')
        self.log_stage(parameterObj)

    def simulate(self, parameterObj):
        print("[#] Preflight...")
        self._preflight_simulate(parameterObj)
        print("[+] Checks passed.")
        #determine name of sims/group
        if not parameterObj.label:
            run_count = self._return_group_last_integer('sims')
            parameterObj.label = f"run_{run_count}"
        self.data.require_group(f'sims/{parameterObj.label}')
        if parameterObj.sim_grid:
            sim_configs = self._get_sim_grid(parameterObj)
        else:
            sim_configs = lib.gimble.DOL_to_LOD(parameterObj.parameter_combinations)
        lib.simulate.run_sim(sim_configs, parameterObj, self)
        try:
            print(self._get_sims_report(width=100, label=parameterObj.label))   
        except UnicodeEncodeError:
            print(self._get_sims_report(width=100, label=parameterObj.label).__repr__().encode('utf-8')) #temp fix for my environment
        self.log_stage(parameterObj)

    def query(self, parameterObj):
        print("[#] Preflight...")
        self._preflight_query(parameterObj)
        print("[#] Query...")
        if parameterObj.data_format == 'bed':
            if parameterObj.data_type == 'blocks':
                print("[#] Writing block BED...")
                self._write_block_bed(parameterObj)
            elif parameterObj.data_type == 'windows':
                print("[#] Writing window BED...")
                self._write_window_bed(parameterObj)
            else:
                raise ValueError("'data_type' must be must be 'blocks' or 'windows'")
        elif parameterObj.data_format == 'simplebed':
            if parameterObj.data_type == 'windows':
                self._write_window_bed(parameterObj, simple=True)
            else:
                raise ValueError("'data_type' must be must be 'blocks' or 'windows'")
        elif parameterObj.data_format == 'bsfs':
            self.dump_bsfs(data_type=parameterObj.data_type, sample_sets='X', kmax_by_mutype=parameterObj.kmax_by_mutype)
        elif parameterObj.data_format == 'lncls':
            self.dump_lncls(parameterObj)
        else:
            sys.exit("[+] Nothing to be done.")

    def dump_lncls(self, parameterObj):
        unique_hash = parameterObj._get_unique_hash()
        grids, grid_meta_dict = self._get_grid(unique_hash)
        lncls_global, lncls_windows = self._get_lncls(unique_hash)
        if isinstance(grid_meta_dict, list):
            values_by_parameter = LOD_to_DOL(grid_meta_dict)
            #values_by_parameter = grid_meta_dict_to_value_arrays_by_parameter(grid_meta_dict)
        else:
            values_by_parameter = grid_meta_dict #once everything works, values_by_parameter should be simply renamed
        sequences, starts, ends, index = self._get_window_bed_columns()
        parameter_names = [name for name in values_by_parameter.keys() if name != 'mu']
        grid_meta_dict.pop('mu', None) #remove mu from grid_meta_dict
        column_headers = ['sequence', 'start', 'end', 'index', 'lnCL'] + parameter_names + ['fixed']
        dtypes = {'start': 'int64', 'end': 'int64', 'index': 'int64', 'lnCL': 'float64'}
        for param in parameter_names:
            dtypes[param] = 'float64'
        MAX_PARAM_LENGTH = max([len(param) for param in parameter_names])
        for parameter in tqdm(parameter_names, total=len(parameter_names), desc="[%] Writing output...", ncols=100, unit_scale=True): 
            bed_dfs = []
            out_f = '%s.%s.gridsearch.lnCls.%s_fixed.tsv' % (self.prefix, parameterObj.data_type, parameter)
            fixed = np.full_like(lncls_windows.shape[0], parameter, dtype='<U%s' % MAX_PARAM_LENGTH)
            for grid_meta_idxs in self.get_slice_grid_meta_idxs(grid_meta_dict=grid_meta_dict, lncls=lncls_windows, fixed_parameter=parameter, parameter_value=None):
                best_likelihoods = lncls_windows[np.arange(lncls_windows.shape[0]), grid_meta_idxs]
                best_params = np.array(list(zip(*grid_meta_dict.values())))[grid_meta_idxs]    
                #line above replaces code until best_params = 
                #meta_dicts = list(np.vectorize(grid_meta_dict.__getitem__)(grid_meta_idxs.astype(str)))
                #columns = []
                #for param in parameter_names:
                #    column = []
                #    for meta_dict in meta_dicts:
                #        column.append(meta_dict[param])
                #    columns.append(column)
                #best_params = np.vstack(columns).T
                int_bed = np.vstack([starts, ends, index, best_likelihoods, best_params.T]).T
                header = ["# %s" % parameterObj._VERSION]
                header += ["# %s" % "\t".join(column_headers)]
                with open(out_f, 'w') as out_fh:
                    out_fh.write("\n".join(header) + "\n")
                bed_df = pd.DataFrame(data=int_bed, columns=column_headers[1:-1]).astype(dtype=dtypes)
                bed_df['sequence'] = sequences
                bed_df['fixed'] = fixed
                # MUST be mode='a' otherwise header gets wiped ...
                bed_dfs.append(bed_df)
            df = pd.concat(bed_dfs, ignore_index=True, axis=0)
            df.sort_values(['index'], ascending=[True]).to_csv(out_f, na_rep='NA', mode='a', sep='\t', index=False, header=False, columns=column_headers)

    def _get_window_bed_columns(self):
        meta_seqs = self._get_meta('seqs')
        meta_windows = self._get_meta('windows')
        MAX_SEQNAME_LENGTH = max([len(seq_name) for seq_name in meta_seqs['seq_names']])
        sequences = np.zeros(meta_windows['count'], dtype='<U%s' % MAX_SEQNAME_LENGTH)
        starts = np.zeros(meta_windows['count'], dtype=np.int64)
        ends = np.zeros(meta_windows['count'], dtype=np.int64)
        offset = 0
        for seq_name in tqdm(meta_seqs['seq_names'], total=len(meta_seqs['seq_names']), desc="[%] Preparing output...", ncols=100, unit_scale=True): 
            start_key = 'windows/%s/starts' % (seq_name)
            end_key = 'windows/%s/ends' % (seq_name)
            if start_key in self.data:
                start_array = np.array(self.data[start_key])
                window_count = start_array.shape[0]
                starts[offset:offset+window_count] = start_array
                ends[offset:offset+window_count] = np.array(self.data[end_key])
                sequences[offset:offset+window_count] = np.full_like(window_count, seq_name, dtype='<U%s' % MAX_SEQNAME_LENGTH)
                offset += window_count
        index = np.arange(meta_windows['count'],  dtype=np.int64)
        return (sequences, starts, ends, index)

    def _write_gridsearch_bed(self, parameterObj=None, lncls=None, best_idx=None, grid_meta_dict=None):
        if parameterObj is None or lncls is None or grid_meta_dict is None:
            raise ValueError('_write_gridsearch_bed: needs parameterObj and lncls and grid_meta_dict')
        #grids = []
        grids = DOL_to_LOD(grid_meta_dict)
        #for grid_idx, grid_dict in grid_meta_dict.items():
        #    grids.append(list(grid_dict.values()))
        grid_params = np.array([list(subdict.values()) for subdict in grids] ,dtype=np.float64)
        best_params = grid_params[np.argmax(lncls, axis=1), :]
        best_likelihoods = np.max(lncls, axis=1)
        delta_lncls = best_likelihoods - lncls[:, best_idx]
        sequences, starts, ends, index = self._get_window_bed_columns() 
        params_header = list(next(iter(grids)).keys())
        #params_header = list(grid_dict.keys())
        meta_blocks = self._get_meta('blocks')
        meta_windows = self._get_meta('windows')
        bsfs_windows_full = self.get_bsfs(
                data_type='windows', 
                population_by_letter=parameterObj.config['population_by_letter'], 
                sample_sets='X',
                kmax_by_mutype=parameterObj.config['k_max']
                )
        popgen_metrics = pop_metrics_from_bsfs(bsfs_windows_full, block_length=meta_blocks['length'], window_size=meta_windows['size'])
        popgen_header = ['heterozygosity_A', 'heterozygosity_B', 'd_xy', 'f_st']
        columns = ['sequence', 'start', 'end', 'index', 'lnCL', 'delta_lnCl'] + params_header + popgen_header
        dtypes = {'start': 'int64', 'end': 'int64', 'index': 'int64', 'lnCL': 'float64', 'delta_lnCl': 'float64'}
        for param in params_header + popgen_header:
            dtypes[param] = 'float64'
        '''dtypes := "object", "int64", "float64", "bool", "datetime64", "timedelta", "category"'''
        int_bed = np.vstack([starts, ends, index, best_likelihoods, delta_lncls, best_params.T, popgen_metrics]).T
        out_f = '%s.%s.gridsearch.bestfit.bed' % (self.prefix, parameterObj.data_type)
        print("[+] Sum of lnCL for winning parameters = %s" % np.sum(best_likelihoods))
        # write header
        header = ["# %s" % parameterObj._VERSION]
        header += ["# %s" % "\t".join(columns)]
        with open(out_f, 'w') as out_fh:
            out_fh.write("\n".join(header) + "\n")
        bed_df = pd.DataFrame(data=int_bed, columns=columns[1:]).astype(dtype=dtypes)
        bed_df['sequence'] = sequences
        # MUST be mode='a' otherwise header gets wiped ...
        bed_df.sort_values(['sequence', 'start'], ascending=[True, True]).to_csv(out_f, na_rep='NA', mode='a', sep='\t', index=False, header=False, columns=columns)
        return out_f

    def get_slice_grid_meta_idxs(self, grid_meta_dict=None, lncls=None, fixed_parameter=None, parameter_value=None):
        '''
        fixed_parameter=None, parameter_value=None  => 1 idx (overall max likelihood gridkey)
        fixed_parameter=str, parameter_value=None   => list of n 1d-arrays of idxs with shape (windows,) (n=unique values of parameter)
        fixed_parameter=str, parameter_value=float  => 1d-array of idxs with shape (windows,) 
        '''
        if fixed_parameter: 
            if isinstance(grid_meta_dict, list):
                values_by_parameter = LOD_to_DOL(grid_meta_dict)
                #values_by_parameter = grid_meta_dict_to_value_arrays_by_parameter(grid_meta_dict)
            else:
                values_by_parameter = {k:np.array(v, dtype=np.float64) for k,v in grid_meta_dict.items()}
            if not fixed_parameter in values_by_parameter:
                raise ValueError("%r is not part of this model" % fixed_parameter)
            fixed_parameter_values = np.array(values_by_parameter[fixed_parameter])
            if parameter_value:
                fixed_parameter_indices = np.concatenate(np.argwhere(parameter_value==fixed_parameter_values))
                if not np.any(fixed_parameter_indices):
                    raise ValueError("parameter_value %r not found in grid" % parameter_value)
                fixed_parameter_lncls = lncls[:, fixed_parameter_indices]
                fixed_parameter_lncls_max_idx = np.argmax(fixed_parameter_lncls, axis=1)        
                fixed_parameter_lncls_max_idx = fixed_parameter_indices[fixed_parameter_lncls_max_idx] #translate back to larger grid_meta_dict idxs
                return fixed_parameter_lncls_max_idx
            results = []
            for i in np.unique(fixed_parameter_values): #these values are sorted
                fixed_parameter_indices = np.concatenate(np.argwhere(i==fixed_parameter_values))
                fixed_parameter_lncls = lncls[:, fixed_parameter_indices]
                fixed_parameter_lncls_max_idx = np.argmax(fixed_parameter_lncls, axis=1)
                idxs = fixed_parameter_indices[fixed_parameter_lncls_max_idx]
                results.append(idxs)
            return results
        return np.argmax(lncls, axis=1) 
        
    def gridsearch(self, parameterObj):
        '''grids.shape = (gridpoints, m1_max+1, m2_max+1, m3_max+1, m4_max+1)

        [To Do] 
        - DRL: let's refactor this once simulate-gridsearch is integrated into this function
            - each data_type should declare lncls and best_idx, these are then used at the end of the function to print 
        - DRL: ask Konrad whether bSFS or sum-wbSFS should be used in block-gridsearch 
        '''
        print("[#] Gridsearching ...")
        # get grid
        unique_hash, params = parameterObj._get_unique_hash(return_dict=True)
        grids, grid_meta_dict = self._get_grid(unique_hash)
        if parameterObj.data_type == 'windows':
            # [windows]
            print('[+] Getting wbSFSs ...')
            bsfs_windows_clipped = self.get_bsfs(
                data_type='windows', 
                population_by_letter=parameterObj.config['population_by_letter'], 
                sample_sets='X', 
                kmax_by_mutype=parameterObj.config['k_max'])
            lncls_windows = self.gridsearch_np(bsfs=bsfs_windows_clipped, grids=grids)
            self._set_lncls(unique_hash, lncls_windows, lncls_type='windows', overwrite=parameterObj.overwrite)
            # gridsearch [global]
            bsfs_windows_clipped_summed = sum_wbsfs(bsfs_windows_clipped)
            lncls_global = self.gridsearch_np(bsfs=bsfs_windows_clipped_summed, grids=grids)
            self._set_lncls(unique_hash, lncls_global, lncls_type='global', overwrite=parameterObj.overwrite)
            best_idx = np.argmax(lncls_global, axis=0)
            print('[+] Best grid point (based on bSFS within windows): %s' % lncls_global[best_idx])
            #extract single best paramcombo
            best_value = [v[best_idx] for v in grid_meta_dict.values()]
            print('[+] \t %s' % "; ".join(["%s = %s" % (k, v) for k, v in zip(grid_meta_dict.keys(), best_value)]))
            #print('[+] \t %s' % "; ".join(["%s = %s" % (k, v) for k, v in grid_meta_dict[str(best_idx)].items()]))
            self._write_gridsearch_bed(parameterObj=parameterObj, lncls=lncls_windows, best_idx=best_idx, grid_meta_dict=grid_meta_dict)
        elif parameterObj.data_type == 'simulate':
            self._gridsearch_sims(parameterObj, grids, grid_meta_dict)
        elif parameterObj.data_type == 'blocks':
            # [blocks]
            print('[+] Getting bSFSs ...')
            bsfs_clipped = self.get_bsfs(
                data_type='blocks', 
                population_by_letter=parameterObj.config['population_by_letter'], 
                sample_sets='X', 
                kmax_by_mutype=parameterObj.config['k_max'])
            lncls_blocks = self.gridsearch_np(bsfs=bsfs_clipped, grids=grids)
            self._set_lncls(unique_hash, lncls_blocks, lncls_type='blocks', overwrite=parameterObj.overwrite)
            best_idx = np.argmax(lncls_blocks, axis=0)
            best_value = [v[best_idx] for v in grid_meta_dict.values()]
            print('[+] Best grid point (based on bSFS): %s' % lncls_blocks[best_idx])
            print('[+] \t %s' % "; ".join(["%s = %s" % (k, v) for k, v in zip(grid_meta_dict.keys(), best_value)]))
            #print('[+] \t %s' % "; ".join(["%s = %s" % (k, v) for k, v in grid_meta_dict[str(best_idx)].items()]))
            print('[+] Warning: gridsearch on bSFS is still experimental!')
        else:
            raise ValueError("Datatype other than windows, blocks or simulate was specified using gridsearch. Should have been caught earlier.")

    def _gridsearch_sims(self, parameterObj, grids, grid_meta_dict):
        #check parameters that were fixed initially:
        #all_dicts = [grid_meta_dict[str(i)] for i in range(len(grid_meta_dict))] 
        ##this can be omitted once parameterObj.parameter_combinations is
        ##in the shape {Ne_A:[v1, v2, v3, v4, ...], Ne_B:[v1', v2' , ...], ...}
        #keys = list(all_dicts[0].keys())
        #key_all_values_dict = {}
        #for key in keys:
        #    key_all_values_dict[key] = np.array([d[key] for d in all_dicts], dtype=np.float64)
        gridded_params = sorted([key for key, items in grid_meta_dict.items() if len(set(items))>1])

        if 'fixed_param_grid' in self.data[f'sims/{parameterObj.label}'].attrs:
            fixed_param_grid = self.data[f'sims/{parameterObj.label}'].attrs['fixed_param_grid']
            fixed_param_grid_value = self.data[f'sims/{parameterObj.label}/parameter_combination_0'].attrs[fixed_param_grid]
            unique_values_fixed_param = np.unique(grid_meta_dict[fixed_param_grid])
            #unique_values_fixed_param = np.unique(key_all_values_dict[fixed_param_grid])            
            fixed_param_grid_value_idx = np.where(unique_values_fixed_param==fixed_param_grid_value)[0][0]
        else:
            fixed_param_grid = None
            fixed_param_grid_value = None
        
        param_combo_iterator = self.get_bsfs(
            data_type='simulate', 
            population_by_letter=parameterObj.config['populations'], 
            sample_sets="X", 
            kmax_by_mutype=parameterObj.config['k_max'],
            label=parameterObj.label
            ) #iterator over each parameter combination that was sim'ed
        num_param_combos = self.data[f'sims/{parameterObj.label}'].__len__()
        for name, data in tqdm(param_combo_iterator, desc='Processing all parameter combinations', total=num_param_combos,  ncols=100):
            df, df_fixed_param = self._gridsearch_sims_single(data, grids, fixed_param_grid, gridded_params, grid_meta_dict, parameterObj.label, name, fixed_param_grid_value_idx)
        
        print(f"[+] Output written to {os.getcwd()}")    
        if fixed_param_grid:
            print("[+] Fixed param values:")
            print('\t'.join(f'{fixed_param_grid}_{i}' if i !=fixed_param_grid_value_idx else f'{fixed_param_grid}_background' for i in range(len(unique_values_fixed_param))))
            print('\t'.join("{:.3e}".format(value) for value in unique_values_fixed_param))

    def _gridsearch_sims_single(self, data, grids, fixed_param_grid, gridded_params, grid_meta_dict, label, name, fixed_param_grid_value_idx):
        assert np.product(data.shape[1:])==np.product(grids.shape[1:]), "Dimensions of sim bSFS and grid bSFS do not correspond. k_max does not correspond but not caught."
        data = np.reshape(data, (data.shape[0],-1)) #data shape: replicates * bSFS
        grids = np.reshape(grids, (grids.shape[0],-1)) #grids shape: num_grid_points * bSFS
        grids_log = np.zeros(grids.shape, dtype=np.float64)
        grids = np.log(grids, where=grids>0, out=grids_log)
        lncls = np.inner(data, grids_log) #result shape: replicates*num_grid_points
        param_idxs = np.argmax(lncls, axis=1) #returns optimal index for each replicate

        results_dict = {}
        for key in gridded_params:
            results_dict[key] = grid_meta_dict[key][param_idxs]
        df = pd.DataFrame(results_dict)
        summary=df.describe(percentiles=[0.025, 0.05, 0.95, 0.975])
        summary.to_csv(f'{label}_{name}_summary.csv')
        df_fixed_param = None

        if fixed_param_grid:
            assert fixed_param_grid in gridded_params, "fixed param for bootstrap not in gridded_params list! Report this issue."
            columns = [] #shape = num_values_fixed_param * replicates
            #values are not sorted!
            for fixed_param_value_idxs in self.get_slice_grid_meta_idxs(grid_meta_dict=grid_meta_dict, lncls=lncls, fixed_parameter=fixed_param_grid):
                best_likelihoods = lncls[np.arange(lncls.shape[0]), fixed_param_value_idxs]
                columns.append(best_likelihoods)
            #results in column are sorted from smallest to largest param value
            columns= np.array(columns).T
            df_fixed_param = pd.DataFrame(columns, columns=[f"{fixed_param_grid}_{str(i)}" if i!=fixed_param_grid_value_idx else f'{fixed_param_grid}_background' for i in range(columns.shape[1])])
            df_fixed_param.to_csv(f'{label}_{name}_lnCL_dist.csv')
        return (df, df_fixed_param)

    def _set_lncls(self, unique_hash, lncls, lncls_type='global', overwrite=False):
        '''lncls_type := 'global' or 'windows'
        '''
        key = "%s/%s" % (unique_hash, lncls_type)
        dataset = self.data['lncls'].create_dataset(key, data=lncls, overwrite=overwrite)

    def _has_lncls(self, unique_hash, legacy=False):
        path = "lncls/%s" % unique_hash if not legacy else "lncls/global/%s" % unique_hash
        if path in self.data:
            return True
        return False

    def _get_lncls(self, unique_hash):
        if self._has_lncls(unique_hash):
            key_global = "lncls/%s/global" % unique_hash
            key_windows = "lncls/%s/windows" % unique_hash
        elif self._has_lncls(unique_hash, legacy=True):
            key_global = "lncls/global/%s" % unique_hash
            key_windows = "lncls/windows/%s" % unique_hash
        else:
            sys.exit("[X] No lnCLs for this INI.")
        lncls_global = np.array(self.data[key_global], dtype=np.float64)
        lncls_windows = np.array(self.data[key_windows], dtype=np.float64)
        return (lncls_global, lncls_windows)

    def optimize(self, parameterObj):
        if not self.has_stage(parameterObj.data_type):
            sys.exit("[X] gimbleStore has no %r." % parameterObj.data_type)
        label = parameterObj.label if hasattr(parameterObj, 'label') else None
        
        if parameterObj.numPoints>1: #numPoints can only be used with blocks
            if parameterObj.data_type != 'blocks':
                print("[-] --n_points cannot be set with datatypes other than blocks. Will be set to 1.")
                parameterObj.numPoints = 1
        data = self.get_bsfs(
            data_type=parameterObj.data_type, 
            population_by_letter=parameterObj.config['populations'], 
            sample_sets="X", 
            kmax_by_mutype=parameterObj.config['k_max'],
            label=label
            )

        #resample blocks and determine for each parameter ftol_abs
        self._set_stopping_criteria(data, parameterObj, label)
        # load math.EquationSystemObj
        equationSystem = lib.math.EquationSystemObj(parameterObj)
        # initiate model equations
        equationSystem.initiate_model(parameterObj)
        #this is for a single dataset
        
        '''
        DRL: is there a way of not making distinction between data_type below?
        GB: Yes, that would be making the distinction between running optimize across
        replicates and running it across multiple starting points. 
        '''
        if parameterObj.data_type=='simulate':
            if parameterObj.trackHistory:
                print("[-] Tracking optimization cannot be enabled when optimising simulations.")
                parameterObj.trackHistory = False
            #data is an iterator over parameter_combination_name, parameter_combination_array
            #each param_comb_array contains n replicates
            all_results={}
            for param_combo, replicates in data:
                print(f"Optimising replicates {param_combo}")
                result = equationSystem.optimize_parameters(replicates, parameterObj, trackHistory=False, verbose=False)
                all_results[param_combo] = result
                self._optimize_to_csv(result, label, param_combo)
        else:
            results = equationSystem.optimize_parameters(
                data, 
                parameterObj,
                trackHistory=True,
                verbose=True
                )

    def _optimize_to_csv(self, results, label, param_combo):
        df = pd.DataFrame(results[1:])
        df.columns=results[0]
        df = df.sort_values(by='iterLabel')
        df.set_index('iterLabel', inplace=True)
        df.to_csv(f'{label}_{param_combo}.csv')
        #print(f"[] Optimize output saved to {os.getcwd()}")
        self._optimize_describe_df(df, label, param_combo)

    def _optimize_describe_df(self, df, label, param_combo):
        summary=df.drop(labels=['lnCL', 'exitcode'], axis=1).describe(percentiles=[0.025,0.975])
        summary.to_csv(f'{label}_{param_combo}_summary.csv')

    def _set_stopping_criteria(self, data, parameterObj, label):
        set_by_user = True
        #if parameterObj.ftol_rel<0:
        #    set_by_user = False
        #    lnCL_sd, lnCL_all_data = self._get_lnCL_SD(data, parameterObj, label)
        #    parameterObj.ftol_rel = abs(1.96*lnCL_sd/lnCL_all_data)
        print("Stopping criteria for this optimization run:")
        print(f"Max number of evaluations: {parameterObj.max_eval}")
        if set_by_user and parameterObj.ftol_rel>0:
            print(f"Relative tolerance on lnCL: {parameterObj.ftol_rel}")
        else:
            pass
            #print(f"Relative tolerance on lnCL set by data resampling: {parameterObj.ftol_rel}")
        if parameterObj.xtol_rel>0:
            print(f"Relative tolerance on norm of parameter vector: {parameterObj.ftol_rel}")
        
    def _get_lnCL_SD(self, all_data, parameterObj, label):
        if parameterObj.data_type == 'windows':
            data = np.sum(all_data, axis=0)
            #if windows transform to sum_windowwise_bsfs
        elif parameterObj.data_type == 'simulate':    
            #if simulate data contains all replicates
            #if datatype=simulate all_data is an iterator! adapt this!
            all_data = self._get_sims_bsfs(label, single=True)
            data = np.sum(all_data, axis=0)
        else:
            data = all_data
        total_num_blocks = np.sum(data)
        ETPs = data/total_num_blocks
        
        if parameterObj.data_type=='simulate':
            #no need to resample, we have enough replicates
            #we treat the total dataset as approximating the truth
            #and turn those into ETPs
            lnCLs_resample = [lib.math.calculate_composite_likelihood(ETPs, replicate) for replicate in all_data]
            replicates = len(lnCLs_resample)
            resample_size = replicates
        else:
            replicates=100
            resample_size=min(1000, total_num_blocks)
            kmax_by_mutype=list(parameterObj.config['k_max'].values())
            resample_bsfs=self._resample_bsfs(data, resample_size, replicates, kmax_by_mutype)
            #get lnCL for each resampled dataset
            lnCLs_resample = [lib.math.calculate_composite_likelihood(ETPs, resample) for resample in resample_bsfs]
            #determine sd of outcome
        sd_mini = np.std(lnCLs_resample, ddof=1)
        sd = sd_mini * np.sqrt((resample_size-1)/(total_num_blocks-1))
        lnCL_all_data = lib.math.calculate_composite_likelihood(ETPs, data)
        return (sd, lnCL_all_data)

    def _resample_bsfs(self, data, resample_size, replicates, kmax_by_mutype):
        bsfs_2d = lib.gimble.bsfs_to_2d(data)
        bsfs_configs = bsfs_2d[:,1:]
        bsfs_probs = bsfs_2d[:,0]
        bsfs_probs=bsfs_probs/np.sum(bsfs_probs)
        resample=np.random.choice(np.arange(bsfs_configs.shape[0]), (replicates, resample_size), replace=True, p=bsfs_probs)
        resample_bsfs=bsfs_configs[resample]
        resample_bsfs=resample_bsfs.reshape(-1,len(kmax_by_mutype))
        index=np.repeat(np.arange(replicates), resample_size).reshape(replicates*resample_size,1)
        resample_bsfs_index=np.concatenate([index, resample_bsfs], axis=1)
        mutuples, counts = np.unique(resample_bsfs_index, return_counts=True, axis=0)
        shape_out = [replicates,]+[k+2 for k in kmax_by_mutype]
        out = np.zeros(tuple(shape_out), np.int64)
        out[tuple(mutuples.T)]=counts
        return out    

    def makegrid(self, parameterObj):
        unique_hash = parameterObj._get_unique_hash()
        if self._has_grid(unique_hash) and not parameterObj.overwrite:
            sys.exit("[X] Grid for this config file already exists.")
        number_grid_points = len(parameterObj.parameter_combinations[next(iter(parameterObj.parameter_combinations))])
        print("[+] Generated %s grid points combinations." % number_grid_points)
        equationSystem = lib.math.EquationSystemObj(parameterObj)
        #build the equations
        equationSystem.initiate_model(parameterObj=parameterObj)
        equationSystem.ETPs = equationSystem.calculate_all_ETPs(threads=parameterObj.threads, gridThreads=parameterObj.gridThreads, verbose=False)
        self._set_grid(unique_hash, equationSystem.ETPs, parameterObj.parameter_combinations, overwrite=parameterObj.overwrite)

    def _set_grid(self, unique_hash, ETPs, grid_labels, overwrite=False):
        dataset = self.data['grids'].create_dataset(unique_hash, data=ETPs, overwrite=overwrite)
        #dataset.attrs.put({idx:combo for idx, combo in enumerate(grid_labels)})
        grid_labels = {k:list(v) for k,v in grid_labels.items()}
        dataset.attrs.put(grid_labels) #using dict of list to save parameters

    def _has_grid(self, unique_hash):
        if f'grids/{unique_hash}' in self.data:
            return True
        return False

    def _get_grid(self, unique_hash):
        '''
        grid.shape = (gridpoints, m1_max+1, m2_max+1, m3_max+1, m4_max+1) 
        '''
        if f'grids/{unique_hash}' in self.data:
            grid_meta = self.data[f'grids/{unique_hash}'].attrs.asdict()
            grid = np.array(self.data[f'grids/{unique_hash}'], dtype=np.float64)
            return (grid, grid_meta)
        else:
            sys.exit("[X] No grid for this INI.")

    def _get_sim_grid(self, parameterObj):
        unique_hash = parameterObj._get_unique_hash(module='makegrid')
        grid_meta_dict = self.data[f'grids/{unique_hash}'].attrs.asdict()
        if not self._has_lncls(unique_hash):
            sys.exit("[X] Run gridsearch -w module first to calculate lnCLs.")
        lncls_global, lncls_windows = self._get_lncls(unique_hash)
        #lncls global should be based on w_bsfs !
        global_winning_fixed_param_idx = np.argmax(lncls_global)
        if parameterObj.fixed_param_grid:
            global_winning_fixed_param_value = grid_meta_dict[parameterObj.fixed_param_grid][global_winning_fixed_param_idx]
            #global_winning_fixed_param_value = grid_meta_dict[str(global_winning_fixed_param_idx)][parameterObj.fixed_param_grid]
        else:
            global_winning_fixed_param_value = None
        #get optimal parametercombo given background for fixed parameter
        if parameterObj.fixed_param_grid:
            local_winning_fixed_param_idx = self.get_slice_grid_meta_idxs(lncls=lncls_windows, grid_meta_dict=grid_meta_dict, fixed_parameter=parameterObj.fixed_param_grid, parameter_value=global_winning_fixed_param_value)
        else:
            local_winning_fixed_param_idx = self.get_slice_grid_meta_idxs(lncls=lncls_windows)
        if isinstance(parameterObj.recombination_map, pd.DataFrame):
            assert(parameterObj.recombination_map.shape[0]==len(local_winning_fixed_param_idx)), "Index recmap and windows not matching. Should have been caught."
            grid_to_sim, window_df = self._get_sim_grid_with_rec_map(parameterObj, local_winning_fixed_param_idx, grid_meta_dict)
        else:
            grid_to_sim, window_df = self._get_sim_grid_fixed_rec(parameterObj, local_winning_fixed_param_idx, grid_meta_dict)

        param_df = pd.DataFrame(grid_to_sim)
        param_df.drop(labels=['mu'], inplace=True, axis=1, errors='ignore')
        param_df.to_csv(f'simulated_grid_{parameterObj.label}.tsv', sep='\t')
        print(f"[+] Wrote simulated_grid_{parameterObj.label}.tsv containing all simulated parameter combinations.")
        window_df.to_csv(f'windows_sims_param_idx_{parameterObj.label}.tsv', sep='\t')
        print(f"[+] Wrote windows_sims_param_idx_{parameterObj.label}.tsv containing all windowwise info.")
        return grid_to_sim

    def _get_sim_grid_with_rec_map(self, parameterObj, local_winning_fixed_param_idx, grid_meta_dict):
        parameterObj.recombination_map['param_idx']=local_winning_fixed_param_idx
        param_for_window = parameterObj.recombination_map[['rec_bins','param_idx']].to_dict(orient='split')['data']
        param_for_window = [tuple(window) for window in param_for_window]
        unique, param_combo_idxs = np.unique(param_for_window, return_inverse=True, axis=0)
        parameterObj.recombination_map['param_with_rec_idx'] = param_combo_idxs
        grid_to_sim = []
        for r, idx in unique:
            #param_dict = copy.deepcopy(grid_meta_dict[str(int(idx))])
            param_dict = {k:v for k,v in zip(grid_meta_dict.keys(),list(zip(*grid_meta_dict.values()))[int(idx)])}
            param_dict['recombination'] = r
            grid_to_sim.append(param_dict)
        window_df = parameterObj.recombination_map[['sequence', 'start', 'end', 'param_with_rec_idx']]
        return (grid_to_sim, window_df)

    def _get_sim_grid_fixed_rec(self, parameterObj, local_winning_fixed_param_idx, grid_meta_dict):
        param_combo_idxs = np.unique(local_winning_fixed_param_idx)
        rec_rate = parameterObj.config["parameters"]["recombination"][0]
        grid_to_sim=[]
        old_to_new_idx = {} #dict to translate old idx to new in grid_to_sim
        for new_idx, old_idx in enumerate(param_combo_idxs):
            old_to_new_idx[old_idx] = new_idx
            #param_dict = copy.deepcopy(grid_meta_dict[str(int(old_idx))])
            param_dict = {k:v for k,v in zip(grid_meta_dict.keys(),list(zip(*grid_meta_dict.values()))[int(old_idx)])}
            param_dict['recombination'] = rec_rate
            grid_to_sim.append(param_dict)
        window_df = self._get_window_coordinates()
        window_df['param_idx'] = [old_to_new_idx[idx] for idx in local_winning_fixed_param_idx]
        return (grid_to_sim, window_df) 

    def _get_window_coordinates(self):
        meta_seqs = self._get_meta('seqs')
        meta_windows = self._get_meta('windows')
        MAX_SEQNAME_LENGTH = max([len(seq_name) for seq_name in meta_seqs['seq_names']])
        sequences = np.zeros(meta_windows['count'], dtype='<U%s' % MAX_SEQNAME_LENGTH)
        starts = np.zeros(meta_windows['count'], dtype=np.int64)
        ends = np.zeros(meta_windows['count'], dtype=np.int64)
        #retrieving window coordinates
        offset = 0
        for seq_name in meta_seqs['seq_names']: 
            start_key = 'windows/%s/starts' % (seq_name)
            end_key = 'windows/%s/ends' % (seq_name)
            if start_key in self.data:
                start_array = np.array(self.data[start_key])
                window_count = start_array.shape[0]
                starts[offset:offset+window_count] = start_array
                ends[offset:offset+window_count] = np.array(self.data[end_key])
                sequences[offset:offset+window_count] = np.full_like(window_count, seq_name, dtype='<U%s' % MAX_SEQNAME_LENGTH)
                offset += window_count
        return pd.DataFrame({'sequence':sequences, 'start':starts, 'end':ends})

    def _validate_seq_names(self, sequences=None):
        """Returns valid seq_names in sequences or raises ValueError."""
        meta = self._get_meta('seqs')
        if not sequences:
            return meta['seq_names']
        if set(sequences).issubset(set(meta['seq_names'])):
            return sequences
        else:
            raise ValueError("%s not a subset of %s" % (sequences, meta['seq_names']))

    def _get_invert_population_flag(self, population_by_letter=None):
        """Returns True if populations need inverting, and False if not or population_by_letter is None. 
        Raises ValueError if population_by_letter of data and config differ"""
        meta = self._get_meta('seqs')
        if population_by_letter:
            if not population_by_letter['A'] in meta['population_by_letter'].values() or not population_by_letter['B'] in meta['population_by_letter'].values():
                sys.exit("[X] Population names in config (%r) and gimble-store (%r) must match" % (str(population_by_letter.values()), str(meta['population_by_letter'].values())))
            if not population_by_letter['A'] == meta['population_by_letter']['A']:
                return True
        return False

    def _get_sample_set_idxs(self, query=None):
        """Returns list of sample_set_idxs.

        Parameters 
        ----------
        query : string or None
                'X' - inter-population sample_sets
                'A' - intra-population sample_sets of population A
                'B' - intra-population sample_sets of population B 
                None - all sample_sets
        
        Returns
        -------
        out : list of strings
            sample_set_idxs that can be used to access data in gimble store
        """
        meta = self._get_meta('seqs')
        if not query:
            return [str(idx) for idx in range(len(meta['sample_sets']))]
        elif query == 'X':
            return [str(idx) for (idx, is_cartesian) in enumerate(meta['sample_sets_inter']) if is_cartesian]
        elif query == 'A':
            return [str(idx) for (idx, is_intra_A) in enumerate(meta['sample_sets_intra_A']) if is_intra_A]
        elif query == 'B':
            return [str(idx) for (idx, is_intra_B) in enumerate(meta['sample_sets_intra_B']) if is_intra_B]
        else:
            raise ValueError("'query' must be 'X', 'A', 'B', or None")

    def _get_window_bsfs(self, sample_sets='X', sequences=None, population_by_letter=None, kmax_by_mutype=None):
        """Return bsfs_array of 5 dimensions (fifth dimension is the window-idx across ALL sequences in sequences).
        [ToDo] Ideally this should work with regions, as in CHR:START-STOP.
        [ToDo] put in sample set context (error if not samples set).
    
        Parameters 
        ----------
        sequences : list of strings or None
            Only make bSFS based on variation on these sequences seq_names.

        population_by_letter : dict (string -> string) or None
            Mapping of population IDs to population letter in model (from INI file).

        kmax : dict (string -> int) or None
            Mapping of kmax values to mutypes.
        
        Returns
        -------
        bsfs : (dask) ndarray, int, ndim (1 + mutypes). First dimension is window idx. 
        """
        sequences = self._validate_seq_names(sequences)
        invert_population_flag = self._get_invert_population_flag(population_by_letter)
        max_k = np.array(list(kmax_by_mutype.values())) + 1 if kmax_by_mutype else np.array([8,8,8,8]) 
        variations = []
        for seq_name in tqdm(sequences, total=len(sequences), desc="[%] Querying data ", ncols=100):
            variation = np.array(self.data["windows/%s/variation" % seq_name], dtype=np.uint16)
            variations.append(variation)
        variation = np.concatenate(variations, axis=0)
        if invert_population_flag:
            variation[:,:,[0, 1]] = variation[:,:,[1, 0]]
        mutuples, counts = np.unique(variation, return_counts=True, axis=0)
        bsfs = variation.reshape((variation.shape[0] * variation.shape[1], variation.shape[2]))
        index = np.repeat(np.arange(variation.shape[0]), variation.shape[1]).reshape(variation.shape[0] * variation.shape[1], 1)
        mutuples, counts = np.unique(
            np.concatenate([index, np.clip(bsfs, 0, max_k)], axis=-1).reshape(-1, bsfs.shape[-1] + 1),
            return_counts=True, axis=0)
        # define out based on max values for each column
        try:
            out = np.zeros(tuple(np.max(mutuples, axis=0) + 1), np.uint16) # set to np.uint16 [0..65535]
        except MemoryError as e:
            sys.exit('[+] Gimble ran out of memory. %s' % str(e))
        # assign values
        out[tuple(mutuples.T)] = counts
        return out
    
    def get_bsfs(self, data_type=None, sequences=None, sample_sets=None, population_by_letter=None, kmax_by_mutype=None, label=None):
        """main method for accessing bsfs
        
        unique hash-keys have to be made based on defining parameters of bsfs, which varies by data_type.         
        """
        params = {k: v for k, v in locals().items() if not k == 'self'}
        params_blocks = ['length', 'span', 'max_missing', 'max_multiallelic']
        params_windows = ['size', 'step']
        data_type_bws = set(['blocks', 'windows', 'windows_sum'])
        data_type_ws = set(['windows', 'windows_sum'])
        if data_type in data_type_bws:
            meta_blocks = self._get_meta('blocks')
            #assert meta_blocks['count_total'] > 0, sys.exit('[X] No blocks found.')
            for key in params_blocks:
                params[key] = meta_blocks[key]
        if data_type in data_type_ws:
            meta_windows = self._get_meta('windows')
            #assert meta_windows['count'] > 0, sys.exit('[X] No windows found.')
            for key in params_windows:
                params[key] = meta_windows[key]
        elif data_type == 'simulate':
            bsfs = self._get_sims_bsfs(label)
            return bsfs
        unique_hash = get_hash_from_dict(params)
        bsfs_data_key = 'bsfs/%s/%s' % (data_type, unique_hash)
        if bsfs_data_key in self.data: 
            # bsfs exists
            return np.array(self.data[bsfs_data_key], dtype=np.int64)
        if data_type == 'blocks':
            bsfs = self._get_block_bsfs(sample_sets=sample_sets, population_by_letter=population_by_letter, kmax_by_mutype=kmax_by_mutype)
        elif data_type == 'windows':
            bsfs = self._get_window_bsfs(sample_sets=sample_sets, population_by_letter=population_by_letter, kmax_by_mutype=kmax_by_mutype)
        elif data_type == 'windows_sum':
            bsfs = sum_wbsfs(self._get_window_bsfs(sample_sets=sample_sets, population_by_letter=population_by_letter, kmax_by_mutype=kmax_by_mutype))
        elif data_type == 'sims':
            raise ValueError("Error in sequence of if/else statements for get_bsfs with simulate.")
        else:
            raise ValueError("data_type must be 'blocks', 'windows', or 'windows_sum")
        if np.any(bsfs):
            meta_bsfs = self._get_meta('bsfs')
            meta_bsfs[unique_hash] = str(params)
            self.data.create_dataset(bsfs_data_key, data=bsfs, overwrite=True)
        return bsfs

    def _get_block_bsfs(self, sequences=None, sample_sets=None, population_by_letter=None, kmax_by_mutype=None):
        """Returns bsfs_array of 4 dimensions.

        Parameters 
        ----------
        sequences : list of strings or None
            If supplied, bSFSs are based only on variation on those sequences
        
        sample_sets : string or None
                None - all sample_sets (default)
                'X' - inter-population sample_sets
                'A' - intra-population sample_sets of population A
                'B' - intra-population sample_sets of population B 
            If supplied, bSFSs are based only on variation of those sample_sets
        
        population_by_letter : dict (string -> string) or None
            Mapping of population IDs to population letter in model (from INI file).

        kmax : dict (string -> int) or None
            Mapping of kmax values to mutypes.

        Returns
        -------
        out : ndarray, int, ndim (mutypes)

        """
        sample_set_idxs = self._get_sample_set_idxs(query=sample_sets)
        sequences = self._validate_seq_names(sequences)
        invert_population_flag = self._get_invert_population_flag(population_by_letter)
        max_k = np.array(list(kmax_by_mutype.values())) + 1 if kmax_by_mutype else None 
        variations = []
        for seq_name in sequences: 
            for sample_set_idx in sample_set_idxs:
                variation_key = 'blocks/%s/%s/variation' % (seq_name, sample_set_idx)
                if variation_key in self.data:
                    variations.append(np.array(self.data[variation_key], dtype=np.int64))
        if not variations:
            return None
        variation = np.concatenate(variations, axis=0) # concatenate is faster than offset-indexes
        if invert_population_flag:
            variation[:, [0, 1]] = variation[:, [1, 0]]
        # count mutuples (clipping at k_max, if supplied)
        mutuples, counts = np.unique(np.clip(variation, 0, max_k), return_counts=True, axis=0)
        # define out based on max values for each column
        out = np.zeros(tuple(np.max(mutuples, axis=0) + 1), np.int64)
        # assign values
        out[tuple(mutuples.T)] = counts
        return out

    def _get_sims_bsfs(self, label, single=False):
        #returns an iterator over parameter_combinations: (name, array) 
        if label:
            if label in self.data['sims']:
                if single:
                    return np.array(self.data[f'sims/{label}/parameter_combination_0'], dtype=np.int32) 
                else:
                    return self.data[f'sims/{label}'].arrays()
            else:
                sys.exit(f"[X] label should be one of {', '.join(self.data['sims'].group_keys())}")
        else:
            if len(self.data['sims']) == 1:
                key=list(self.data['sims'].group_keys())[0]
                if single:    
                    return np.array(self.data[f'sims/{key}/parameter_combination_0'], dtype=np.int32) 
                else:
                    return self.data[f'sims/{key}'].arrays()
            else:
                sys.exit(f"[X] Specify label. Should be one of {', '.join(self.data['sims'].group_keys())}")

    def gridsearch_np(self, bsfs=None, grids=None):
        '''returns 2d array of likelihoods of shape (windows, grids)'''
        if grids is None or bsfs is None:
            raise ValueError('gridsearch: needs grid and data')
        grids_log = np.zeros(grids.shape)
        np.log(grids, where=grids>0, out=grids_log)
        if bsfs.ndim == 4:
            return np.squeeze(np.apply_over_axes(np.sum, (bsfs * grids_log), axes=[-4,-3,-2,-1]))
        return np.squeeze(np.apply_over_axes(np.sum, (bsfs[:, None] * grids_log), axes=[-4,-3,-2,-1]))

    def _get_setup_report(self, width):
        meta_seqs = self._get_meta('seqs')
        reportObj = ReportObj(width=width)
        reportObj.add_line(prefix="[+]", left='[', center='Setup', right=']', fill='=')
        reportObj.add_line(prefix="[+]", left='seqs')
        right = "%s in %s sequence(s) (n50 = %s)" % (
            format_bases(sum(meta_seqs['seq_lengths'])), 
            format_count(len(meta_seqs['seq_lengths'])), 
            format_bases(meta_seqs['seq_n50']))
        reportObj.add_line(prefix="[+]", branch="T", fill=".", left='genome', right=right)
        right = "%s samples in %s populations" % (format_count(len(meta_seqs['samples'])), format_count(len(meta_seqs['population_ids'])))
        reportObj.add_line(prefix="[+]", branch="T", left='populations', fill=".", right=right)
        sample_counts_by_population = collections.Counter(meta_seqs['populations'])
        for idx, (letter, population_id) in enumerate(meta_seqs['population_by_letter'].items()):
            left = "%s = %s" % (letter, population_id)
            right = "%s" % format_count(sample_counts_by_population[population_id])
            branch = "P%s" % ("F" if idx == len(meta_seqs['population_by_letter']) -1 else "T")
            reportObj.add_line(prefix="[+]", branch=branch, left=left, right=right)
        reportObj.add_line(prefix="[+]", branch="T", left="sample sets", fill=".", right=format_count(len(meta_seqs['sample_sets'])))
        reportObj.add_line(prefix="[+]", branch="PT", left="INTER-population sample-sets (X)", right=format_count(len(self._get_sample_set_idxs(query='X'))))
        reportObj.add_line(prefix="[+]", branch="PT", left="INTRA-population sample-sets (A)", right=format_count(len(self._get_sample_set_idxs(query='A'))))
        reportObj.add_line(prefix="[+]", branch="PF", left="INTRA-population sample-sets (B)", right=format_count(len(self._get_sample_set_idxs(query='B'))))
        reportObj.add_line(prefix="[+]", branch="T", left="variants", fill=".", right=("%s (%s per 1 kb)" % (
               format_count(meta_seqs['variants_counts']),
               format_proportion(1000 * meta_seqs['variants_counts'] / sum(meta_seqs['seq_lengths'])))))
        reportObj.add_line(prefix="[+]", branch="PP", right="".join([c.rjust(8) for c in ["HOMREF", "HOMALT", "HET", "MISS"]]))
        for idx, sample in enumerate(meta_seqs['samples']):
            variant_idx = meta_seqs['variants_idx_by_sample'][sample]
            branch = "PF" if idx == len(meta_seqs['variants_idx_by_sample']) - 1 else "PT"
            left = sample
            right = "%s %s %s %s" % (
            (format_percentage(meta_seqs['variants_counts_hom_ref'][variant_idx] / meta_seqs['variants_counts']) if meta_seqs['variants_counts'] else format_percentage(0)).rjust(7),
            (format_percentage(meta_seqs['variants_counts_hom_alt'][variant_idx] / meta_seqs['variants_counts']) if meta_seqs['variants_counts'] else format_percentage(0)).rjust(7),
            (format_percentage(meta_seqs['variants_counts_het'][variant_idx] / meta_seqs['variants_counts']) if meta_seqs['variants_counts'] else format_percentage(0)).rjust(7),
            (format_percentage(meta_seqs['variants_counts_missing'][variant_idx] / meta_seqs['variants_counts']) if meta_seqs['variants_counts'] else format_percentage(0)).rjust(7))
            reportObj.add_line(prefix="[+]", branch=branch, left=left, right=right)
        reportObj.add_line(prefix="[+]", branch="F", left='intervals', fill=".", 
            right = "%s intervals across %s (%s of genome)" % (
                format_count(meta_seqs['intervals_count']),
                format_bases(meta_seqs['intervals_span']),
                format_percentage(meta_seqs['intervals_span'] / sum(meta_seqs['seq_lengths']))))
        return reportObj
    
    def _get_storage_report(self, width):
        reportObj = ReportObj(width=width)
        reportObj.add_line(prefix="[+]", left='[', center='Storage', right=']', fill='=')
        total_size = recursive_get_size(self.path)
        total_right = "%s | %s" % (format_bytes(total_size), format_percentage(total_size/total_size))
        reportObj.add_line(prefix="[+]", left=pathlib.Path(self.path).name, right=total_right, fill='.')
        for idx, group in enumerate(self.data):
            size = recursive_get_size(pathlib.Path(self.path) / pathlib.Path(group))
            percentage = format_percentage(size/total_size)
            branch = branch="T" if idx < len(self.data) - 1 else 'F'
            reportObj.add_line(prefix="[+]", branch=branch, left=group, right='%s | %s' % (format_bytes(size), percentage.rjust(7)), fill='.')
        return reportObj

    def _get_blocks_report(self, width):
        meta_seqs = self._get_meta('seqs')
        meta_blocks = self._get_meta('blocks')
        reportObj = ReportObj(width=width)
        reportObj.add_line(prefix="[+]", left='[', center='Blocks', right=']', fill='=')
        if self.has_stage('blocks'):
            blocks_count_total = sum(meta_blocks['count_by_sample_set_idx'].values())
            blocks_raw_count_total = sum(meta_blocks['count_raw_by_sample_set_idx'].values())
            block_validity = blocks_count_total / blocks_raw_count_total if blocks_count_total else 0
            reportObj.add_line(prefix="[+]", left='blocks')
            reportObj.add_line(prefix="[+]", branch='T', fill=".", 
                left="'-l %s -m %s -u %s -i %s'" % (meta_blocks['length'],meta_blocks['span'],meta_blocks['max_missing'],meta_blocks['max_multiallelic']),
                right=' %s blocks (%s discarded)' % (format_count(blocks_count_total), format_percentage(1 - block_validity)))
            column_just = 14
            if blocks_count_total:
                reportObj.add_line(prefix="[+]", branch="P", right="".join([c.rjust(column_just) for c in ["X", "A", "B"]]))
                bsfs_X = bsfs_to_2d(self.get_bsfs(data_type='blocks', sample_sets='X'))
                bsfs_A = bsfs_to_2d(self.get_bsfs(data_type='blocks', sample_sets='A'))
                bsfs_B = bsfs_to_2d(self.get_bsfs(data_type='blocks', sample_sets='B'))
                interval_coverage_X = meta_blocks['length'] * np.sum(bsfs_X[:,0]) / meta_seqs['intervals_span'] / len(self._get_sample_set_idxs("X")) if np.any(bsfs_X) else "N/A"
                interval_coverage_A = meta_blocks['length'] * np.sum(bsfs_A[:,0]) / meta_seqs['intervals_span'] / len(self._get_sample_set_idxs("A")) if np.any(bsfs_A) else "N/A"
                interval_coverage_B = meta_blocks['length'] * np.sum(bsfs_B[:,0]) / meta_seqs['intervals_span'] / len(self._get_sample_set_idxs("B")) if np.any(bsfs_B) else "N/A"
                reportObj.add_line(prefix="[+]", branch='T', left='interval coverage', right="".join(
                    [format_percentage(c).rjust(column_just) for c in [interval_coverage_X,interval_coverage_A, interval_coverage_B]]))
                total_blocks_X = np.sum(bsfs_X[:,0]) if np.any(bsfs_X) else 0
                total_blocks_A = np.sum(bsfs_A[:,0]) if np.any(bsfs_A) else 0
                total_blocks_B = np.sum(bsfs_B[:,0]) if np.any(bsfs_B) else 0
                reportObj.add_line(prefix="[+]", branch='T', left='total blocks', right="".join([format_count(c).rjust(column_just) for c in [total_blocks_X, total_blocks_A, total_blocks_B]]))
                invariant_blocks_X = bsfs_X[0,0] / np.sum(bsfs_X[:,0]) if np.any(bsfs_X) else "N/A"
                invariant_blocks_A = bsfs_A[0,0] / np.sum(bsfs_A[:,0]) if np.any(bsfs_A) else "N/A"
                invariant_blocks_B = bsfs_B[0,0] / np.sum(bsfs_B[:,0]) if np.any(bsfs_B) else "N/A"
                reportObj.add_line(prefix="[+]", branch='T', left='invariant blocks', right="".join([format_percentage(c).rjust(column_just) for c in [invariant_blocks_X, invariant_blocks_A, invariant_blocks_B]]))
                fgv_blocks_X = np.sum(bsfs_X[(bsfs_X[:,3]>0) & (bsfs_X[:,4]>0)][:,0]) / np.sum(bsfs_X[:,0]) if np.any(bsfs_X) else "N/A"
                fgv_blocks_A = np.sum(bsfs_A[(bsfs_A[:,3]>0) & (bsfs_A[:,4]>0)][:,0]) / np.sum(bsfs_A[:,0]) if np.any(bsfs_A) else "N/A"
                fgv_blocks_B = np.sum(bsfs_B[(bsfs_B[:,3]>0) & (bsfs_B[:,4]>0)][:,0]) / np.sum(bsfs_B[:,0]) if np.any(bsfs_B) else "N/A"
                reportObj.add_line(prefix="[+]", branch='T', left='four-gamete-violation blocks', right="".join([format_percentage(c, precision=2).rjust(column_just) for c in [fgv_blocks_X, fgv_blocks_A, fgv_blocks_B]]))
                X_hetB = np.sum(bsfs_X[:,0] * bsfs_X[:,1]) 
                X_hetA = np.sum(bsfs_X[:,0] * bsfs_X[:,2])
                X_hetAB = np.sum(bsfs_X[:,0] * bsfs_X[:,3]) 
                X_fixed = np.sum(bsfs_X[:,0] * bsfs_X[:,4])
                heterozygosity_XA = (X_hetA + X_hetAB) / (meta_blocks['length'] * np.sum(bsfs_X[:,0]))
                heterozygosity_XB = (X_hetB + X_hetAB) / (meta_blocks['length'] * np.sum(bsfs_X[:,0]))
                dxy_X = ((X_hetA + X_hetB + X_hetAB) / 2.0 + X_fixed) / (meta_blocks['length'] * np.sum(bsfs_X[:,0]))
                mean_pi = (heterozygosity_XA + heterozygosity_XB) / 2.0
                total_pi = (dxy_X + mean_pi) / 2.0 
                fst_X = (dxy_X - mean_pi) / (dxy_X + mean_pi) if (total_pi) else np.nan
                if np.any(bsfs_A):
                    A_hetB = np.sum(bsfs_A[:,0] * bsfs_A[:,1])
                    A_hetA = np.sum(bsfs_A[:,0] * bsfs_A[:,2])
                    A_hetAB = np.sum(bsfs_A[:,0] * bsfs_A[:,3])
                    A_fixed = np.sum(bsfs_A[:,0] * bsfs_A[:,4])
                    total_SA = np.sum(bsfs_A[:,0, None] * bsfs_A[:,1:])
                pi_A = float(fractions.Fraction(1, 2) * (A_hetA + A_hetB) + fractions.Fraction(2, 3) * (A_hetAB + A_fixed)) / (meta_blocks['length'] * np.sum(bsfs_A[:,0])) if np.any(bsfs_A) else "N/A"
                watterson_theta_A = total_SA / float(harmonic(3)) / (meta_blocks['length'] * np.sum(bsfs_A[:,0])) if np.any(bsfs_A) else "N/A"
                heterozygosity_A = (A_hetA + A_hetAB) / (meta_blocks['length'] * np.sum(bsfs_A[:,0])) if np.any(bsfs_A) else "N/A"
                if np.any(bsfs_B):
                    B_hetB = np.sum(bsfs_B[:,0] * bsfs_B[:,1])
                    B_hetA = np.sum(bsfs_B[:,0] * bsfs_B[:,2])
                    B_hetAB = np.sum(bsfs_B[:,0] * bsfs_B[:,3])
                    B_fixed = np.sum(bsfs_B[:,0] * bsfs_B[:,4])
                    total_SB = np.sum(bsfs_B[:,0, None] * bsfs_B[:,1:])
                pi_B = float(fractions.Fraction(1, 2) * (B_hetA + B_hetB) + fractions.Fraction(2, 3) * (B_hetAB + B_fixed)) / (meta_blocks['length'] * np.sum(bsfs_B[:,0])) if np.any(bsfs_B) else "N/A"
                watterson_theta_B = total_SB / float(harmonic(3)) / (meta_blocks['length'] * np.sum(bsfs_B[:,0])) if np.any(bsfs_B) else "N/A"
                heterozygosity_B = (B_hetA + B_hetAB) / (meta_blocks['length'] * np.sum(bsfs_B[:,0])) if np.any(bsfs_B) else "N/A"
                reportObj.add_line(prefix="[+]", branch='T', left='heterozygosity (A)', right="".join([format_proportion(c, precision=5).rjust(column_just) for c in [heterozygosity_XA, heterozygosity_A, '-']]))
                reportObj.add_line(prefix="[+]", branch='T', left='heterozygosity (B)', right="".join([format_proportion(c, precision=5).rjust(column_just) for c in [heterozygosity_XB,'-', heterozygosity_B]]))
                reportObj.add_line(prefix="[+]", branch='T', left='D_xy', right="".join([format_proportion(c, precision=5).rjust(column_just) for c in [dxy_X,'-', '-']]))
                reportObj.add_line(prefix="[+]", branch='T', left='F_st', right="".join([format_proportion(c, precision=5).rjust(column_just) for c in [fst_X,'-', '-']]))
                reportObj.add_line(prefix="[+]", branch='T', left='Pi', right="".join([format_proportion(c, precision=5).rjust(column_just) for c in ['-',pi_A, pi_B]]))
                reportObj.add_line(prefix="[+]", branch='F', left='Watterson theta', right="".join([format_proportion(c, precision=5).rjust(column_just) for c in ['-',watterson_theta_A, watterson_theta_B]]))
        return reportObj

    def _get_windows_report(self, width):
        meta_windows = self._get_meta('windows')
        reportObj = ReportObj(width=width)
        reportObj.add_line(prefix="[+]", left='[', center='Windows', right=']', fill='=')
        if self.has_stage('windows'):
            reportObj.add_line(prefix="[+]", left='windows')
            reportObj.add_line(prefix="[+]", branch='F', fill=".", left="'-w %s -s %s'" % (meta_windows['size'], meta_windows['step']), right=' %s windows' % (
                format_count(meta_windows['count'])))
        return reportObj

    def info(self, tree=False):
        '''
        single instance data/stages: setup/blocks/windows/ 
        multiple-instance data/stages : bsfs/grids/lncls/sims
        '''
        width = 100
        if tree:
            return self.data.tree()
        report = self._get_storage_report(width)
        # single instance data/stages
        if self.has_stage('setup'):
            report += self._get_setup_report(width)    
        report += self._get_blocks_report(width)
        report += self._get_windows_report(width)
        report += self._get_grids_report(width)
        report += self._get_lncls_report(width)
        #report += self._get_bsfs_report(width)
        #report += self._get_sims_report(width)
        # multiple instance data
        return report

    def _get_grids_report(self, width):
        reportObj = ReportObj(width=width)
        reportObj.add_line(prefix="[+]", left='[', center='Grids', right=']', fill='=')
        for grid_id in self.data['grids/']:
            grid_path = 'grids/%s' % grid_id
            grid_dict = self.data[grid_path].attrs.asdict()
            reportObj.add_line(prefix="[+]", branch='W', fill=".", left=grid_id, right=' %s grid points' % format_count(len(grid_dict)))
            # also needs kmax, populations? sync? YES...
            table = []
            for param_name, param_values in grid_dict.items():
                table.append([param_name] + [min(param_values), max(param_values), len(param_values)])
            rows = tabulate.tabulate(table, numalign="right", headers=['param', 'min', 'max', 'n']).split("\n")
            for i, row in enumerate(rows):
                branch = 'F' if (i + 1) == len(rows) else 'P'
                reportObj.add_line(prefix="[+]", branch=branch, fill="", left=row, right='')
        return reportObj

    def _get_lncls_report(self, width):
        reportObj = ReportObj(width=width)
        reportObj.add_line(prefix="[+]", left='[', center='lnCLs', right=']', fill='=')
        for lncls_id in self.data['lncls/']:
            windows_key = 'lncls/%s/%s' % (lncls_id, 'windows')
            global_key = 'lncls/%s/%s' % (lncls_id, 'global')
            lncls_window_shape = np.array(self.data[windows_key]).shape
            lncls_global_shape = np.array(self.data[global_key]).shape
            grid_points = lncls_global_shape[0] if lncls_global_shape else lncls_window_shape[-1]
            reportObj.add_line(prefix="[+]", branch='W', fill=".", left=lncls_id, right=' %s grid points' % format_count(grid_points))
            # also needs kmax, populations? sync? YES...
            table = [
                ['global', lncls_global_shape],
                ['windows', lncls_window_shape]
                ] 
            rows = tabulate.tabulate(table, numalign="right", headers=['datatype', 'shape' ]).split("\n")
            for i, row in enumerate(rows):
                branch = 'F' if (i + 1) == len(rows) else 'P'
                reportObj.add_line(prefix="[+]", branch=branch, fill="", left=row, right='')
        return reportObj

    def _get_sims_report(self, width, label):
        meta_sims = self._get_meta('sims')
        reportObj = ReportObj(width=width)
        reportObj.add_line(prefix="[+]", left='[', center='Sims', right=']', fill='=')
        column_just = 14
        if self.has_stage('simulate'):
            for name, array in self.get_bsfs(data_type='simulate', label=label):#returns an iterator over parametercombination names and arrays
                bsfs_X = bsfs_to_2d(np.array(array))
                fgv_blocks_X = np.sum(bsfs_X[(bsfs_X[:,4]>0) & (bsfs_X[:,5]>0)][:,0]) / np.sum(bsfs_X[:,1]) if np.any(bsfs_X) else "N/A"
                reportObj.add_line(prefix="[+]", branch='T', left=f'four-gamete-violation {name}', right="".join([format_percentage(c, precision=2).rjust(column_just) for c in [fgv_blocks_X,]]))
        return reportObj

    def _count_groups(self, name):
        '''DRL: is this being used?'''
        return len(list(self.data[name]))

    def _init_store(self, create, overwrite):
        if create:
            if os.path.isdir(self.path):
                print("[-] Directory %r already exists." % self.path)
                if not overwrite:
                    print("[X] Please specify '-f' to overwrite.")
                    sys.exit(1)
                print("[+] Deleting existing directory %r" % self.path)
                shutil.rmtree(self.path)
            print("[+] Creating GStore in %r" % self.path)
            return zarr.open(str(self.path), mode='w')
        #print("[+] Loading GStore from %r" % self.path)
        return zarr.open(str(self.path), mode='r+')
    
    def _get_meta(self, stage):
        if stage in self.data:
            return self.data[stage].attrs
        return None

    def _wipe_stage(self, stage):
        if stage in self.data:
            self.data.create_group(stage, overwrite=True)
            self.data[stage].attrs.put(copy.deepcopy(META_TEMPLATE_BY_STAGE[stage]))
        if stage in self.data.attrs:
            del self.data.attrs[stage]

    def _init_meta(self, overwrite=False):
        '''
        groups get overwritten, templates are applied via copy.deepcopy 
        '''
        for stage, meta_template in META_TEMPLATE_BY_STAGE.items():
            self.data.require_group(stage, overwrite=overwrite)
            self.data[stage].attrs.put(copy.deepcopy(META_TEMPLATE_BY_STAGE[stage]))

    #def _is_zarr_group(self, name, subgroup=None):
    #    '''needed?'''
    #    if not subgroup:
    #        return name in list(self.data.group_keys())
    #    else:
    #        return name in list(self.data[subgroup].group_keys())

    def _return_group_last_integer(self, name):
        '''needed? yes, lib.simulate.py'''
        try:
            all_groups = [int([namestring for namestring in groupnames.split('_')][-1]) for groupnames in list(self.data[name]) if groupnames.startswith('run')]
        except KeyError:
            return 0
        if len(all_groups):
            return max(all_groups)+1
        else:
            return 0

    def _set_sequences(self, parameterObj):
        meta = self._get_meta('seqs')
        sequences_df = parse_csv(
            csv_f=parameterObj.genome_f, 
            sep="\t", 
            usecols=[0,1], 
            dtype={'sequence_id': 'category', 'sequence_length': 'int64'}, 
            header=None)
        meta['seq_names'] = sequences_df['sequence_id'].to_list()
        meta['seq_lengths'] = sequences_df['sequence_length'].to_list()
        meta['seq_n50'] = get_n50_from_lengths(meta['seq_lengths'])
        for sequence_id in meta['seq_names']:
            self.data.create_group('seqs/%s/' % sequence_id)
        meta['genome_f'] = parameterObj.genome_f

    def _set_samples(self, parameterObj):
        meta = self._get_meta('seqs')
        samples_df = parse_csv(
            csv_f=parameterObj.sample_f, 
            sep=",", 
            usecols=[0,1], 
            dtype={'samples': 'object', 'populations': 'category'}, 
            header=None)
        meta['samples'] = samples_df['samples'].to_list()
        meta['populations'] = samples_df['populations'].to_list()
        meta['population_ids'] = sorted(set(samples_df['populations'].to_list()))
        meta['population_by_letter'] = {letter: population_id for population_id, letter in zip(meta['population_ids'], string.ascii_uppercase)}
        meta['population_by_sample'] = {sample: population for sample, population in zip(meta['samples'], meta['populations'])}
        meta['sample_sets'] = [
            tuple(sorted(x, key=(meta['population_by_sample'].get if meta['population_by_sample'][x[0]] != meta['population_by_sample'][x[1]] else None))) 
                for x in itertools.combinations(meta['population_by_sample'].keys(), 2)]
        longest_sample_string = max([len(", ".join(sample_set)) for sample_set in meta['sample_sets']]) + 2
        meta['spacing'] = longest_sample_string if longest_sample_string > meta['spacing'] else meta['spacing']
        meta['sample_sets_inter'] = [
            False if len(set([meta['population_by_sample'][sample] for sample in sample_set])) == 1 else True 
                for sample_set in meta['sample_sets']]
        meta['sample_sets_intra_A'] = [
            all([meta['population_by_sample'][sample] == meta['population_ids'][0] for sample in sample_set]) for sample_set in meta['sample_sets']]
        meta['sample_sets_intra_B'] = [
            all([meta['population_by_sample'][sample] == meta['population_ids'][1] for sample in sample_set]) for sample_set in meta['sample_sets']]
        meta['sample_f'] = parameterObj.sample_f

    def _set_variants(self, parameterObj):
        meta = self._get_meta('seqs')
        sequences = meta['seq_names']
        samples = meta['samples']
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            gt_key, pos_key, sample_key = 'calldata/GT', 'variants/POS', 'samples'
            samples_gt_order = allel.read_vcf(parameterObj.vcf_f, fields=[sample_key])[sample_key]
            query_samples = ordered_intersect(a=samples_gt_order, b=samples, order='a')
            # Check if all samples were found
            if set(query_samples) != set(samples):
                sys.exit("[X] The following samples in SAMPLE_FILE were not found in VCF_FILE: %s" % (
                    ", ".join(list(set(samples).difference(set(query_samples))))
                    ))
            # Set up counts arrays
            count_shape = (len(meta['seq_names']), len(query_samples))
            count_records = np.zeros(count_shape[0], dtype=np.int64)
            count_called = np.zeros(count_shape, dtype=np.int64)
            count_hom_ref = np.zeros(count_shape, dtype=np.int64)
            count_hom_alt = np.zeros(count_shape, dtype=np.int64)
            count_het = np.zeros(count_shape, dtype=np.int64)
            count_missing = np.zeros(count_shape, dtype=np.int64)
            for idx, sequence in tqdm(enumerate(sequences), total=len(sequences), desc="[%] Reading variants...", ncols=100):
                vcf_data = allel.read_vcf(parameterObj.vcf_f, region=sequence, samples=query_samples, fields=[gt_key, pos_key])
                if vcf_data:
                    # genotypes
                    gt_matrix = vcf_data[gt_key]
                    # counts
                    sa_genotype_matrix = allel.GenotypeArray(gt_matrix)
                    count_records[idx] = gt_matrix.shape[0]
                    count_called[idx,:] = sa_genotype_matrix.count_called(axis=0)
                    count_hom_ref[idx,:] = sa_genotype_matrix.count_hom_ref(axis=0)
                    count_hom_alt[idx,:] = sa_genotype_matrix.count_hom_alt(axis=0)
                    count_het[idx,:] = sa_genotype_matrix.count_het(axis=0)
                    count_missing[idx,:] = sa_genotype_matrix.count_missing(axis=0)
                    # positions
                    pos_array = check_unique_pos(vcf_data[pos_key] - 1) # port to BED (0-based) coordinates
                    self.data.create_dataset("seqs/%s/variants/pos" % sequence, data=pos_array, dtype=np.int64)
                    self.data.create_dataset("seqs/%s/variants/matrix" % sequence, data=gt_matrix, dtype=np.int64)
            meta['variants_idx_by_sample'] = {query_sample: idx for idx, query_sample in enumerate(query_samples)}
        meta['vcf_f'] = parameterObj.vcf_f
        meta['variants_counts'] = int(np.sum(count_records)) # ZARR JSON encoder does not like numpy dtypes
        meta['variants_counts_called'] = [int(x) for x in np.sum(count_called, axis=0)] # ZARR JSON encoder does not like numpy dtypes
        meta['variants_counts_hom_ref'] = [int(x) for x in np.sum(count_hom_ref, axis=0)] # ZARR JSON encoder does not like numpy dtypes
        meta['variants_counts_hom_alt'] = [int(x) for x in np.sum(count_hom_alt, axis=0)] # ZARR JSON encoder does not like numpy dtypes
        meta['variants_counts_het'] = [int(x) for x in np.sum(count_het, axis=0)] # ZARR JSON encoder does not like numpy dtypes
        meta['variants_counts_missing'] = [int(x) for x in np.sum(count_missing, axis=0)] # ZARR JSON encoder does not like numpy dtypes
        # QC plots 

    def _set_intervals(self, parameterObj):
        meta = self._get_meta('seqs')
        query_sequences = set(meta['seq_names'])
        df = parse_csv(
            csv_f=parameterObj.bed_f, 
            sep="\t", 
            usecols=[0, 1, 2, 4], 
            dtype={'sequence': 'category', 'start': 'int64', 'end': 'int64', 'samples': 'category'},
            header=None)
        intervals_df = df[df['sequence'].isin(set(meta['seq_names']))].sort_values(['sequence', 'start'], ascending=[True, True]).reset_index(drop=True)
        intervals_df = pd.concat([intervals_df, intervals_df.samples.str.get_dummies(sep=',').filter(meta['samples'])], axis=1).drop(columns=['samples'])
        intervals_df_samples = [sample for sample in intervals_df.columns[3:]]
        query_samples = ordered_intersect(a=intervals_df_samples, b=meta['samples'], order='a')
        intervals_df['length'] = (intervals_df['end'] - intervals_df['start'])
        # Check if all samples were found
        if set(query_samples) != set(meta['samples']):
                sys.exit("[X] The following samples in SAMPLE_FILE were not found in BED_FILE: %s" % (
                    ", ".join(list(set(meta['samples_sorted']).difference(set(query_samples))))))
        # Set up counts arrays
        count_shape = (len(meta['seq_names']), len(query_samples))
        count_bases_samples = np.zeros(count_shape, dtype=np.int64)
        for idx, (sequence, _df) in tqdm(enumerate(intervals_df.groupby(['sequence'], observed=True)), total=len(query_sequences), desc="[%] Reading intervals...", ncols=100):
            interval_matrix = _df[query_samples].to_numpy()
            length_matrix = np.repeat(_df['length'].to_numpy(), interval_matrix.shape[1]).reshape(interval_matrix.shape)
            length_matrix[interval_matrix == 0] = 0 # sets length to 0 if interval not present in interval_matrix 
            count_bases_samples[idx,:] = np.sum(length_matrix, axis=0)
            self.data.create_dataset("seqs/%s/intervals/matrix" % sequence, data=interval_matrix)
            self.data.create_dataset("seqs/%s/intervals/starts" % sequence, data=_df['start'].to_numpy())
            self.data.create_dataset("seqs/%s/intervals/ends" % sequence, data=_df['end'].to_numpy())
        meta['intervals_span_sample'] = [int(x) for x in np.sum(count_bases_samples, axis=0)] # JSON encoder does not like numpy dtypes   
        meta['intervals_count'] = len(intervals_df.index)
        meta['intervals_span'] = int(intervals_df['length'].sum())
        meta['intervals_idx_by_sample'] = {sample: idx for idx, sample in enumerate(query_samples)}
        meta['bed_f'] = parameterObj.bed_f
        # QC plots
        #intervals_df['distance'] = np.where((intervals_df['sequence'] == intervals_df['sequence'].shift(-1)), (intervals_df['start'].shift(-1) - intervals_df['end']) + 1, np.nan)
        #distance_counter = intervals_df['distance'].dropna(how="any", inplace=False).value_counts()
        #length_counter = intervals_df['length'].value_counts()
        #distance_f = "%s.intervals.distance.png" % parameterObj.outprefix
        #plot_loglog(distance_counter, 'Distance to downstream BED interval', distance_f)
        #length_f = "%s.intervals.length.png" % parameterObj.outprefix
        #plot_loglog(length_counter, 'Length of BED interval', length_f)
        #count_sequences = intervals_df['sequence'].nunique()
        #count_intervals = len(intervals_df.index)
        #count_samples = len(query_samples)

    def get_bsfs_filename(self, data_type=None, sequences=None, sample_sets=None, population_by_letter=None, kmax_by_mutype=None):
        if data_type == 'blocks':
            meta_blocks = self._get_meta('blocks')
            return "%s.l_%s.m_%s.i_%s.u_%s.bsfs.%s.tsv" % (self.prefix, meta_blocks['length'], meta_blocks['span'], meta_blocks['max_missing'], meta_blocks['max_multiallelic'], data_type)
        elif data_type == 'windows':
            meta_blocks = self._get_meta('blocks')
            meta_windows = self._get_meta('windows')
            return "%s.l_%s.m_%s.i_%s.u_%s.w_%s.s_%s.bsfs.%s.tsv" % (self.prefix, meta_blocks['length'], meta_blocks['span'], meta_blocks['max_missing'], meta_blocks['max_multiallelic'], meta_windows['size'], meta_windows['step'], data_type)
        elif data_type == 'windows_sum':
            meta_blocks = self._get_meta('blocks')
            meta_windows = self._get_meta('windows')
            return "%s.l_%s.m_%s.i_%s.u_%s.w_%s.s_%s.bsfs.%s.tsv" % (self.prefix, meta_blocks['length'], meta_blocks['span'], meta_blocks['max_missing'], meta_blocks['max_multiallelic'], meta_windows['size'], meta_windows['step'], data_type)
        else:
            raise ValueError("data_type %s is not defined" % data_type)

    def dump_bsfs(self, data_type=None, sequences=None, sample_sets=None, population_by_letter=None, kmax_by_mutype=None):
        meta_seqs = self._get_meta('seqs')
        header = ['count'] + [x + 1 for x in range(meta_seqs['mutypes_count'])]
        bsfs_type = '%s-bSFS' % ('b' if data_type == 'blocks' else 's')
        print("[#] Getting %s ..." % bsfs_type)
        bsfs_2d = bsfs_to_2d(
            self.get_bsfs(
                data_type=data_type, 
                sequences=sequences, 
                sample_sets=sample_sets, 
                population_by_letter=population_by_letter, 
                kmax_by_mutype=kmax_by_mutype))
        bsfs_marginality = calculate_bsfs_marginality(bsfs_2d, kmax_by_mutype=kmax_by_mutype) # float, proportion of data summarised at that kmax_by_mutype
        print('[+] Proportion of %s-data in marginals (w/ kmax = %s) = %s' % (bsfs_type, list(kmax_by_mutype.values()) if kmax_by_mutype else None, bsfs_marginality))
        bsfs_filename = self.get_bsfs_filename(
            data_type=data_type,
            sequences=sequences,
            sample_sets=sample_sets,
            population_by_letter=population_by_letter,
            kmax_by_mutype=kmax_by_mutype
            )
        print("[#] Writing %s ..." % bsfs_type)
        pd.DataFrame(data=bsfs_2d, columns=header, dtype='int64').to_csv(bsfs_filename, index=False, sep='\t')

    def _plot_blocks(self, parameterObj):
        # mutuple barchart
        # meta = self.data['seqs'].attrs
        mutypes_inter_key = 'seqs/bsfs/inter/mutypes' 
        counts_inter_key = 'seqs/bsfs/inter/counts' 
        mutypes_inter = self.data[mutypes_inter_key]
        counts_inter = self.data[counts_inter_key]
        self.plot_bsfs_pcp('%s.bsfs_pcp.png' % self.prefix, mutypes_inter, counts_inter)

    def _write_window_bed(self, parameterObj, simple=False):
        '''[OUTPUT function]'''
        meta_seqs = self._get_meta('seqs')
        meta_windows = self._get_meta('windows')
        meta_blocks = self._get_meta('blocks')
        #sample_set_idxs = [idx for (idx, is_cartesian) in enumerate(meta_seqs['sample_sets_inter']) if is_cartesian] if cartesian_only else range(len(meta_seqs['sample_sets']))
        MAX_SEQNAME_LENGTH = max([len(seq_name) for seq_name in meta_seqs['seq_names']])
        sequences = np.zeros(meta_windows['count'], dtype='<U%s' % MAX_SEQNAME_LENGTH)
        starts = np.zeros(meta_windows['count'], dtype=np.int64)
        ends = np.zeros(meta_windows['count'], dtype=np.int64)
        index = np.arange(meta_windows['count'])
        offset = 0
        for seq_name in tqdm(meta_seqs['seq_names'], total=len(meta_seqs['seq_names']), desc="[%] Preparing data...", ncols=100, unit_scale=True): 
            start_key = 'windows/%s/starts' % (seq_name)
            end_key = 'windows/%s/ends' % (seq_name)
            if start_key in self.data:
                start_array = np.array(self.data[start_key])
                window_count = start_array.shape[0]
                starts[offset:offset+window_count] = start_array
                ends[offset:offset+window_count] = np.array(self.data[end_key])
                sequences[offset:offset+window_count] = np.full_like(window_count, seq_name, dtype='<U%s' % MAX_SEQNAME_LENGTH)
                offset += window_count
        if simple:
            columns = ['sequence', 'start', 'end', 'index']
            dtypes = {'start': 'int64', 'end': 'int64', 'index': 'int64'}
            int_bed = np.vstack([starts, ends, index]).T
            out_f = '%s.windows.bed' % self.prefix
        else:
            columns = ['sequence', 'start', 'end', 'index', 'heterozygosity_A', 'heterozygosity_B', 'd_xy', 'f_st']
            dtypes = {'start': 'int64', 'end': 'int64', 'index': 'int64', 
                'heterozygosity_A': 'float64', 'heterozygosity_B': 'float64', 'd_xy': 'float64', 'f_st': 'float64'}
            bsfs_windows_full = self.get_bsfs(data_type='windows', sample_sets='X')
            pop_metrics = pop_metrics_from_bsfs(bsfs_windows_full, block_length=meta_blocks['length'], window_size=meta_windows['size'])
            int_bed = np.vstack([starts, ends, index, pop_metrics]).T
            out_f = '%s.windows.popgen.bed' % self.prefix
        header = ["# %s" % parameterObj._VERSION]
        header += ["# %s" % "\t".join(columns)]  
        
        with open(out_f, 'w') as out_fh:
            out_fh.write("\n".join(header) + "\n")
        # bed
        bed_df = pd.DataFrame(data=int_bed, columns=columns[1:]).astype(dtype=dtypes)
        bed_df['sequence'] = sequences
        bed_df.sort_values(['sequence', 'start'], ascending=[True, True]).to_csv(out_f, na_rep='NA', mode='a', sep='\t', index=False, header=False, columns=columns, float_format='%.5f')
        return out_f

    def _write_block_bed(self, parameterObj, sample_sets='X'):
        '''[OUTPUT function]'''
        meta_seqs = self._get_meta('seqs')
        meta_blocks = self._get_meta('blocks')
        sample_set_idxs = self._get_sample_set_idxs(query=sample_sets)
        blocks_count_total = sum([meta_blocks['count_by_sample_set_idx'][idx] for idx in sample_set_idxs])
        starts = np.zeros(blocks_count_total, dtype=np.int64)
        ends = np.zeros(blocks_count_total, dtype=np.int64)
        # dynamically set string dtype for sequence names
        MAX_SEQNAME_LENGTH = max([len(seq_name) for seq_name in meta_seqs['seq_names']])
        sequences = np.zeros(blocks_count_total, dtype='<U%s' % MAX_SEQNAME_LENGTH) 
        sample_sets = np.zeros(blocks_count_total, dtype=np.int64) 
        variation = np.zeros((blocks_count_total, meta_seqs['mutypes_count']), dtype=np.int64)
        missing = np.zeros(blocks_count_total, dtype=np.int64) 
        multiallelic = np.zeros(blocks_count_total, dtype=np.int64) 
        with tqdm(total=(len(meta_seqs['seq_names']) * len(sample_set_idxs)), desc="[%] Preparing data...", ncols=100, unit_scale=True) as pbar: 
            offset = 0
            for seq_name in meta_seqs['seq_names']: 
                for sample_set_idx in sample_set_idxs:
                    start_key = 'blocks/%s/%s/starts' % (seq_name, sample_set_idx)
                    end_key = 'blocks/%s/%s/ends' % (seq_name, sample_set_idx)
                    if start_key in self.data:
                        start_array = np.array(self.data[start_key])
                        block_count = start_array.shape[0]
                        starts[offset:offset+block_count] = start_array
                        ends[offset:offset+block_count] = np.array(self.data[end_key])
                        sequences[offset:offset+block_count] = np.full_like(block_count, seq_name, dtype='<U%s' % MAX_SEQNAME_LENGTH)
                        sample_sets[offset:offset+block_count] = np.full_like(block_count, sample_set_idx)
                        variation_key = 'blocks/%s/%s/variation' % (seq_name, sample_set_idx)
                        missing_key = 'blocks/%s/%s/missing' % (seq_name, sample_set_idx)
                        multiallelic_key = 'blocks/%s/%s/missing' % (seq_name, sample_set_idx)
                        variation[offset:offset+block_count] = np.array(self.data[variation_key])
                        missing[offset:offset+block_count] = np.array(self.data[missing_key]).flatten()
                        multiallelic[offset:offset+block_count] = np.array(self.data[multiallelic_key]).flatten()
                        offset += block_count
                    pbar.update()
        columns = ['sequence', 'start', 'end', 'sample_set']
        int_bed = np.vstack([starts, ends, sample_sets, missing, multiallelic, variation.T]).T
        mutypes_count = ["m_%s" % str(x+1) for x in range(meta_seqs['mutypes_count'])]
        columns += ['missing', 'multiallelic'] + mutypes_count    
        # header
        header = ["# %s" % parameterObj._VERSION]
        header += ["# %s = %s" % (sample_set_idx, ", ".join(meta_seqs['sample_sets'][int(sample_set_idx)])) for sample_set_idx in sample_set_idxs] 
        header += ["# %s" % "\t".join(columns)]  
        out_f = '%s.blocks.bed' % self.prefix
        with open(out_f, 'w') as out_fh:
            out_fh.write("\n".join(header) + "\n")
        # bed
        bed_df = pd.DataFrame(data=int_bed, columns=columns[1:])
        bed_df['sequence'] = sequences
        bed_df.sort_values(['sequence', 'start'], ascending=[True, True]).to_csv(out_f, na_rep='NA', mode='a', sep='\t', index=False, header=False, columns=columns, float_format='%.5f')

    def _preflight_query(self, parameterObj):
        if parameterObj.data_type == 'blocks':
            if not self.has_stage('blocks'):
                sys.exit("[X] GStore %r has no blocks. Please run 'gimble blocks'." % self.path)
        if parameterObj.data_type == 'windows' or parameterObj.data_type == 'windows_sum':
            if not self.has_stage('windows'):
                sys.exit("[X] GStore %r has no windows. Please run 'gimble windows'." % self.path)

    def _preflight_windows(self, parameterObj):
        if not self.has_stage('blocks'):
            sys.exit("[X] GStore %r has no blocks. Please run 'gimble blocks'." % self.path)
        if self.has_stage('windows'):
            if not parameterObj.overwrite:
                sys.exit("[X] GStore %r already contains windows.\n[X] These windows => %r\n[X] Please specify '--force' to overwrite." % (self.path, self.get_stage('windows')))
            print('[-] GStore %r already contains windows. But these will be overwritten...' % (self.path))
            self._wipe_stage('windows')
    
    def _preflight_blocks(self, parameterObj):
        if not self.has_stage('setup'):
            sys.exit("[X] GStore %r has no data. Please run 'gimble setup'." % self.path)
        if self.has_stage('blocks'):
            if not parameterObj.overwrite:
                sys.exit("[X] GStore %r already contains blocks.\n[X] These blocks => %r\n[X] Please specify '--force' to overwrite." % (self.path, self.get_stage('blocks')))
            print('[-] GStore %r already contains blocks. But these will be overwritten...' % (self.path))
            # wipe bsfs, windows, AND meta, since new blocks...
            self._wipe_stage('blocks')
            self._wipe_stage('windows')
            self._wipe_stage('bsfs')

    def _preflight_simulate(self, parameterObj):
        if 'sims' not in self.data.group_keys():
            self._init_meta(overwrite=False, module='sims')
        for group in self.data['sims'].group_keys():
            if not bool(self.data[f'sims/{group}']):
                del self.data[f'sims/{group}']
        if parameterObj.label in self.data['sims'].group_keys():
            sys.exit(f"[X] There already is a simulation run labeled {parameterObj.label}")
        if parameterObj.sim_grid:
            if isinstance(parameterObj.recombination_map, pd.DataFrame):
                parameterObj.recombination_map = parameterObj._validate_recombination_map(self, parameterObj.recombination_map)
            unique_hash = parameterObj._get_unique_hash(module='makegrid')
            if not self._has_grid(unique_hash):
                sys.exit("[X] Provided config file does not correspond to an existing grid.")

    #def dump_windows(self, parameterObj):
    #    window_info_rows = []
    #    window_mutuple_tally = []
    #    for sequence_id in tqdm(self.data.attrs['sequence_ids'], total=len(self.data.attrs['sequence_ids']), desc="[%] Generating output ", ncols=100):
    #        variations = self.data["%s/windows/variation" % sequence_id]
    #        #print(self.data["%s/windows/starts" % sequence_id][:])
    #        #print(self.data["%s/windows/pos_mean" % sequence_id][:])
    #        #print(self.data["%s/windows/pos_median" % sequence_id][:])
    #        window_ids = np.array(["_".join([sequence_id, _start, _end]) for (_start, _end) in zip(
    #            np.array(self.data["%s/windows/starts" % sequence_id]).astype(str), 
    #            np.array(self.data["%s/windows/ends" % sequence_id]).astype(str))])
    #        #window_ids = self.data["%s/windows/window_id" % sequence_id]
    #        midpoint_means = self.data["%s/windows/pos_mean" % sequence_id]
    #        midpoint_medians = self.data["%s/windows/pos_median" % sequence_id]
    #        for window_id, variation, midpoint_mean, midpoint_median in zip(window_ids, variations, midpoint_means, midpoint_medians):
    #            pi_1, pi_2, d_xy, f_st, fgv = calculate_popgen_from_array(variation, (self.data.attrs['block_length'] * variation.shape[0]))
    #            window_info_rows.append([window_id, sequence_id, midpoint_mean, midpoint_median, pi_1, pi_2, d_xy, f_st, fgv/variation.shape[0]])
    #            # mutuple barchart
    #            mutypes, counts = np.unique(variation, return_counts=True, axis=0)
    #            tally = np.concatenate([counts[:, np.newaxis], mutypes], axis =-1)
    #            windows = np.array([window_id] * tally.shape[0])
    #            window_mutuple_tally.append(np.concatenate([windows[:, np.newaxis], tally], axis =-1))
    #    window_bsfs_cols = ['window_id', 'count'] + [x+1 for x in range(self.data.attrs['mutypes_count'])]
    #    window_bsfs_df = pd.DataFrame(np.vstack(window_mutuple_tally), columns=window_bsfs_cols)
    #    print("[+] Made %s windows" % window_bsfs_df['window_id'].nunique()) 
    #    window_bsfs_f = "%s.window_bsfs.tsv" % self.prefix
    #    window_bsfs_df.to_csv(window_bsfs_f, sep='\t', index=False)
    #    print("[>] Created: %r" % str(window_bsfs_f))
    #
    #    window_info_cols = ['window_id', 'sequence_id', 'midpoint_mean', 'midpoint_median', 'pi_%s' % self.data.attrs['pop_ids'][0], 'pi_%s' % self.data.attrs['pop_ids'][1], 'd_xy', 'f_st', 'fgv']
    #    window_info_df = pd.DataFrame(window_info_rows, columns=window_info_cols)
    #    window_info_f = "%s.window_info.tsv" % self.prefix
    #    window_info_df.to_csv(window_info_f, sep='\t', index=False)
    #    print("[>] Created: %r" % str(window_info_f))        
    #    self.plot_fst_genome_scan(window_info_df)
    #    self.plot_pi_genome_scan(window_info_df)
    #    #     plot_pi_scatter(window_df, '%s.pi_scatter.png' % parameterObj.dataset)
    
    #def plot_pi_genome_scan(self, window_df):
    #    offset_by_sequence_id = {}
    #    offset = 0
    #    x_boundaries = []
    #    for sequence_id, sequence_length in zip(self.data.attrs['sequence_ids'], self.data.attrs['sequence_length']):
    #        offset_by_sequence_id[sequence_id] = offset
    #        x_boundaries.append(offset)
    #        offset += sequence_length
    #    x_boundaries.append(offset)
    #    #print([(sequenceObj.id, sequenceObj.length) for sequenceObj in sequenceObjs])
    #    #print(x_boundaries)
    #    fig = plt.figure(figsize=(18,4), dpi=200, frameon=True)
    #    #connecting dots
    #    ax = fig.add_subplot(111)  
    #    window_df['rel_pos'] = window_df['midpoint_median'] + window_df['sequence_id'].map(offset_by_sequence_id)
    #    window_df.sort_values(['rel_pos'], inplace=True)
    #    #print(window_df)
    #    pi_A_key = list(window_df.columns)[4]
    #    pi_B_key = list(window_df.columns)[5]
    #    ax.plot(window_df['rel_pos'], window_df[pi_A_key], color='orange', alpha=0.8, linestyle='-', linewidth=1, label=pi_A_key.replace('pi_', ''))
    #    ax.plot(window_df['rel_pos'], window_df[pi_B_key], color='dodgerblue', alpha=0.8, linestyle='-', linewidth=1, label=pi_B_key.replace('pi_', ''))
    #    y_lim = (min(window_df[pi_A_key].min(), window_df[pi_B_key].min()), max(window_df[pi_A_key].max(), window_df[pi_B_key].max()))
    #    ax.vlines(x_boundaries, y_lim[0], y_lim[1], colors=['lightgrey'], linestyles='dashed', linewidth=1)
    #    ax.set_ylim(y_lim)
    #    ax.spines['right'].set_visible(False)
    #    ax.spines['top'].set_visible(False)
    #    ax.legend(numpoints=1)
    #    plt.ylabel('Pi')
    #    plt.xlabel("Genome coordinate")
    #    out_f = '%s.pi_genome_scan.png' % self.prefix
    #    plt.tight_layout()
    #    fig.savefig(out_f, format="png")
    #    print("[>] Created: %r" % str(out_f))
    #    plt.close(fig)
    #
    #def plot_fst_genome_scan(self, window_df):
    #    offset_by_sequence_id = {}
    #    offset = 0
    #    x_boundaries = []
    #    for sequence_id, sequence_length in zip(self.data.attrs['sequence_ids'], self.data.attrs['sequence_length']):
    #        offset_by_sequence_id[sequence_id] = offset
    #        x_boundaries.append(offset)
    #        offset += sequence_length
    #    x_boundaries.append(offset)
    #    fig = plt.figure(figsize=(18,4), dpi=200, frameon=True)
    #    #connecting dots
    #    ax = fig.add_subplot(111)  
    #    y_lim = (0.0, 1.0)
    #    window_df['rel_pos'] = window_df['midpoint_median'] + window_df['sequence_id'].map(offset_by_sequence_id)
    #    window_df.sort_values(['rel_pos'], inplace=True)
    #    ax.plot(window_df['rel_pos'], window_df['f_st'], color='lightgrey', alpha=0.8, linestyle='-', linewidth=1)
    #    scatter = ax.scatter(window_df['rel_pos'], window_df['f_st'], c=window_df['d_xy'], alpha=1.0, cmap='PiYG_r', edgecolors='white', marker='o', s=40, linewidth=0.2)
    #    cbar = fig.colorbar(scatter, ax=ax)
    #    cbar.ax.set_title('D_xy')
    #    ax.vlines(x_boundaries, 0.0, 1.0, colors=['lightgrey'], linestyles='dashed', linewidth=1)
    #    ax.set_ylim(y_lim)
    #    ax.spines['right'].set_visible(False)
    #    ax.spines['top'].set_visible(False)
    #    plt.ylabel('F_st')
    #    plt.xlabel("Genome coordinate")
    #    ax.autoscale_view(tight=None, scalex=True, scaley=True)
    #    out_f = '%s.fst_genome_scan.png' % self.prefix
    #    fig.savefig(out_f, format="png")
    #    plt.close(fig)
    #    print("[>] Created: %r" % str(out_f))

    def _make_windows(self, parameterObj, sample_sets='X'):
        meta_seqs = self._get_meta('seqs')
        meta_windows = self._get_meta('windows')
        meta_windows['size'] = parameterObj.window_size
        meta_windows['step'] = parameterObj.window_step
        meta_windows['count'] = 0
        sample_set_idxs = self._get_sample_set_idxs(query=sample_sets)
        with tqdm(meta_seqs['seq_names'], total=(len(meta_seqs['seq_names']) * len(sample_set_idxs)), desc="[%] Constructing windows ", ncols=100, unit_scale=True) as pbar: 
            for seq_name in meta_seqs['seq_names']:
                variation, starts, ends = [], [], []
                for sample_set_idx in sample_set_idxs:
                    variation_key = 'blocks/%s/%s/variation' % (seq_name, sample_set_idx)
                    if variation_key in self.data:
                        variation.append(np.array(self.data[variation_key]))
                        start_key = 'blocks/%s/%s/starts' % (seq_name, sample_set_idx)
                        starts.append(np.array(self.data[start_key]))
                        end_key = 'blocks/%s/%s/ends' % (seq_name, sample_set_idx)
                        ends.append(np.array(self.data[end_key]))
                    pbar.update()
                variation_array = np.concatenate(variation, axis=0)
                start_array = np.concatenate(starts, axis=0)
                end_array = np.concatenate(ends, axis=0)
                # window_variation : shape = (windows, blocklength, 4)
                window_variation, window_starts, window_ends, window_pos_mean, window_pos_median = cut_windows(variation_array, sample_set_idxs, start_array, end_array, num_blocks=parameterObj.window_size, num_steps=parameterObj.window_step)
                #b, counts = np.unique(variation, return_counts=True, axis=0)
                self.data.create_dataset("windows/%s/variation" % seq_name, data=window_variation, overwrite=True)
                self.data.create_dataset("windows/%s/starts" % seq_name, data=window_starts, overwrite=True)
                self.data.create_dataset("windows/%s/ends" % seq_name, data=window_ends, overwrite=True)
                self.data.create_dataset("windows/%s/pos_mean" % seq_name, data=window_pos_mean, overwrite=True)
                self.data.create_dataset("windows/%s/pos_median" % seq_name, data=window_pos_median, overwrite=True)
                meta_windows['count'] += window_variation.shape[0]

        #window_info_rows = []
        #window_mutuple_tally = []
        ## window bsfs
        #for seq_name in tqdm(meta['seq_names'], total=len(meta['seq_names']), desc="[%] Generating output ", ncols=100):
        #    variations = self.data["seqs/%s/windows/variation" % seq_name]
        #    print('variations', variations.shape, variations.info, variations.hexdigest())
        #    for window_id, variation, midpoint_mean, midpoint_median in zip(window_ids, variations, midpoint_means, midpoint_medians):
        #        pi_1, pi_2, d_xy, f_st, fgv = calculate_popgen_from_array(variation, (meta['blocks_length'] * variation.shape[0]))
        #        blocks = variation.shape[0]
        #        window_info_rows.append([window_id, seq_name, midpoint_mean, midpoint_median, pi_1, pi_2, d_xy, f_st, fgv, blocks])
        #        # bsfs for each window
        #        mutuple, counts = np.unique(variation, return_counts=True, axis=0)
        #        tally = np.concatenate([counts[:, np.newaxis], mutuple], axis =-1)
        #        windows = np.array([window_id] * tally.shape[0])
        #        window_mutuple_tally.append(np.concatenate([windows[:, np.newaxis], tally], axis =-1))
        #window_bsfs_cols = ['window_id', 'count'] + [x+1 for x in range(meta['mutypes_count'])]
        #print(window_bsfs_cols)
        #print(window_mutuple_tally)
        #window_bsfs_df = pd.DataFrame(np.vstack(window_mutuple_tally), columns=window_bsfs_cols)
        #print("[+] Made %s windows" % window_bsfs_df['window_id'].nunique()) 
        #window_bsfs_f = "%s.window_bsfs.tsv" % self.prefix
        #window_bsfs_df.to_csv(window_bsfs_f, sep='\t', index=False)
        #print("[>] Created: %r" % str(window_bsfs_f))
        #window_info_cols = ['window_id', 'seq_name', 'midpoint_mean', 'midpoint_median', 'pi_%s' % meta['population_ids'][0], 'pi_%s' % meta['population_ids'][1], 'd_xy', 'f_st', 'f']
        #window_info_df = pd.DataFrame(window_info_rows, columns=window_info_cols)
        #window_info_f = "%s.window_info.tsv" % self.prefix
        #window_info_df.to_csv(window_info_f, sep='\t', index=False)
        #print("[>] Created: %r" % str(window_info_f))
        #self.plot_fst_genome_scan(window_info_df)
        #self.plot_pi_genome_scan(window_info_df)
        ##     plot_pi_scatter(window_df, '%s.pi_scatter.png' % parameterObj.dataset)

    def _get_interval_coordinates_for_sample_set(self, seq_name='', sample_set=[]):
        meta_seqs = self._get_meta('seqs')
        sample_set_key = np.array([meta_seqs['intervals_idx_by_sample'][sample] for sample in sample_set]) # order should not matter...
        matrix_key = 'seqs/%s/intervals/matrix' % seq_name
        start_key = 'seqs/%s/intervals/starts' % seq_name
        end_key = 'seqs/%s/intervals/ends' % seq_name
        if matrix_key in self.data:
            mask = np.all(np.array(self.data[matrix_key])[:,sample_set_key], axis=1)
            return (np.array(self.data[start_key])[mask], np.array(self.data[end_key])[mask])
        return None

    def _make_blocks(self, parameterObj, debug=False):
        meta_seqs = self._get_meta('seqs')
        meta_blocks = self._get_meta('blocks')
        meta_blocks['length'] = parameterObj.block_length
        meta_blocks['span'] = parameterObj.block_span
        meta_blocks['gap_run'] = parameterObj.block_gap_run
        meta_blocks['max_missing'] = parameterObj.block_max_missing
        meta_blocks['max_multiallelic'] = parameterObj.block_max_multiallelic
        blocks_raw_by_sample_set_idx = collections.Counter()   # all possible blocks
        blocks_by_sample_set_idx = collections.Counter()       # all valid blocks => only these get saved to store
        with tqdm(total=(len(meta_seqs['seq_names']) * len(meta_seqs['sample_sets'])), desc="[%] Building blocks ", ncols=100, unit_scale=True) as pbar:        
            for seq_name in meta_seqs['seq_names']:
                pos_key = "seqs/%s/variants/pos" % (seq_name)
                gt_key = "seqs/%s/variants/matrix" % (seq_name)
                pos = np.array(self.data[pos_key], dtype=np.int64) if pos_key in self.data else None
                sa_genotype_array = allel.GenotypeArray(self.data[gt_key].view(read_only=True)) if gt_key in self.data else None
                for sample_set_idx, sample_set in enumerate(meta_seqs['sample_sets']):
                    start_end = self._get_interval_coordinates_for_sample_set(seq_name=seq_name, sample_set=sample_set)
                    if not start_end is None:
                        # Cut sample-set specific blocks based on intervals and block-algoritm parameters
                        starts, ends = start_end
                        block_sites = cut_blocks(starts, ends, meta_blocks['length'], meta_blocks['span'], meta_blocks['gap_run'])
                        if not block_sites is None and np.any(block_sites):
                            # Allocate starts/ends before overwriting position ints
                            block_starts = np.array(block_sites[:, 0], dtype=np.int64)
                            block_ends = np.array(block_sites[:, -1] + 1, dtype=np.int64)
                            # variants take longer than blocking
                            if np.any(pos) or pos is not None:
                                ##print('pos', pos.shape, pos)
                                idx_pos_in_block_sites = np.isin(pos, block_sites, assume_unique=True)
                                #print('idx_pos_in_block_sites', idx_pos_in_block_sites)
                                if np.any(idx_pos_in_block_sites):
                                    sample_set_vcf_idxs = [meta_seqs['variants_idx_by_sample'][sample] for sample in sample_set]
                                    idx_block_sites_in_pos = np.isin(block_sites, pos, assume_unique=True) 
                                    sa_sample_set_genotype_array = sa_genotype_array.subset(idx_pos_in_block_sites, sample_set_vcf_idxs)
                                    block_sites = genotype_to_mutype_array(sa_sample_set_genotype_array, idx_block_sites_in_pos, block_sites, debug)
                                else:
                                    block_sites[:] = 2 # if no variants, set all to invariant    
                            else:
                                block_sites[:] = 2 # if no variants, set all to invariant
                            multiallelic, missing, monomorphic, variation = block_sites_to_variation_arrays(block_sites)
                            valid = (np.less_equal(missing, meta_blocks['max_missing']) & np.less_equal(multiallelic, meta_blocks['max_multiallelic'])).flatten()
                            blocks_raw_by_sample_set_idx[sample_set_idx] += valid.shape[0]
                            blocks_by_sample_set_idx[sample_set_idx] += valid[valid==True].shape[0]
                            blocks_starts_key = 'blocks/%s/%s/starts' % (seq_name, sample_set_idx)
                            self.data.create_dataset(blocks_starts_key, data=block_starts[valid], overwrite=True)
                            blocks_ends_key = 'blocks/%s/%s/ends' % (seq_name, sample_set_idx)
                            self.data.create_dataset(blocks_ends_key, data=block_ends[valid], overwrite=True)
                            blocks_variation_key = 'blocks/%s/%s/variation' % (seq_name, sample_set_idx)
                            self.data.create_dataset(blocks_variation_key, data=variation[valid], overwrite=True)
                            blocks_missing_key = 'blocks/%s/%s/missing' % (seq_name, sample_set_idx)
                            self.data.create_dataset(blocks_missing_key, data=missing[valid], overwrite=True)
                            blocks_multiallelic_key = 'blocks/%s/%s/multiallelic' % (seq_name, sample_set_idx)
                            self.data.create_dataset(blocks_multiallelic_key, data=multiallelic[valid], overwrite=True)
                    pbar.update(1)
        meta_blocks['count_by_sample_set_idx'] = dict(blocks_by_sample_set_idx) # keys are strings
        meta_blocks['count_raw_by_sample_set_idx'] = dict(blocks_raw_by_sample_set_idx) # keys are strings
        meta_blocks['count'] = sum([count for count in blocks_by_sample_set_idx.values()])
    #def _make_blocks_threaded(self, parameterObj, debug=False, threads=2):
    #    '''there might be some speed improvement here ... has to be finished...'''
    #    meta = self.data['seqs'].attrs
    #    meta['blocks_length'] = parameterObj.block_length
    #    meta['blocks_span'] = parameterObj.block_span
    #    meta['blocks_gap_run'] = parameterObj.block_gap_run
    #    meta['blocks_max_missing'] = parameterObj.block_max_missing
    #    meta['blocks_max_multiallelic'] = parameterObj.block_max_multiallelic
    #    blocks_raw_by_sample_set_idx = collections.Counter()   # all possible blocks
    #    blocks_by_sample_set_idx = collections.Counter()       # all valid blocks => only these get saved to store
    #    params = [(seqs, str(sample_seq_idx)) for seqs, sample_seq_idx in itertools.product(meta['seq_names'], range(0, len(meta['sample_sets'])))]
    #    print(params)
    #    results = []
    #    with poolcontext(processes=threads) as pool:
    #        with tqdm(total=len(params), desc="[%] ", ncols=100, unit_scale=True) as pbar:
    #            for blockObjs in pool.imap_unordered(block_algorithm, params):
    #                results.append(blockObjs)
    #                pbar.update()
#
    #    with tqdm(total=(len(meta['seq_names']) * len(meta['sample_sets'])), desc="[%] Building blocks ", ncols=100, unit_scale=True) as pbar:        
    #        for seq_name in meta['seq_names']:        
    #            pos_key = "seqs/%s/variants/pos" % (seq_name)
    #            gt_key = "seqs/%s/variants/matrix" % (seq_name)
    #            for sample_set_idx, (sample_set, sample_set_cartesian) in enumerate(zip(meta['sample_sets'], meta['sample_sets_inter'])):
    #                params.append(seq_name, sample_set_idx, pos_key, gt_key)
    #                pbar.update(1)
    #    meta['blocks_by_sample_set_idx'] = dict(blocks_by_sample_set_idx) # keys are strings
    #    meta['blocks_raw_by_sample_set_idx'] = dict(blocks_raw_by_sample_set_idx) # keys are strings

    #def plot_bsfs_pcp(self, sample_set='X'):
    #    '''plots a bsfs pcp for a given sample_set. 
    #    '''
    #    # https://stackoverflow.com/questions/8230638/parallel-coordinates-plot-in-matplotlib
    #    # http://www.shengdongzhao.com/publication/tracing-tuples-across-dimensions-a-comparison-of-scatterplots-and-parallel-coordinate-plots/
    #    #meta = self.data['seqs'].attrs
    #    bsfs = bsfs_to_2d(self._get_block_bsfs(sample_sets='X'))
    #    print(bsfs)
    #    freq = bsfs[:,0] / np.sum(bsfs[:,0])
    #    meta = self.data['seqs'].attrs
    #    x = ['m_1', 'm_2', 'm_3', 'm_4']
    #    bins = np.linspace(0, 1, 9)
    #    freq = bins[np.digitize(freq, bins)]
    #    data = bsfs[:,1:] 
    #    cmap = plt.get_cmap("Greys")
    #    print('data', data.shape, data)
    #    print('freq', freq.shape, freq)
    #    fig, axes = plt.subplots(1, 3, sharey=False, figsize=(15,5))
    #    axes[0].plot(x,data.T, c=cmap(freq))
    #    axes[1].plot(x,data.T, c=cmap(freq))
    #    axes[2].plot(x,data.T, c=cmap(freq))
    #    plt.subplots_adjust(wspace=0)
    #    #     min_max_range[mutype] = [np.min(, df[col].max(), np.ptp(df[col])]
    #    #         df[col] = np.true_divide(df[col] - df[col].min(), np.ptp(df[col]))
    #    # ynames = ['m_%s' for idx in range(1, meta['mutypes_count'] + 1)]
    #    # import pandas as pd
    #    # from pandas.tools.plotting import parallel_coordinates
    #    # ax = pd.tools.plotting.parallel_coordinates(mutypes)
#
    #    # #ax.plot(window_df['rel_pos'], window_df[pi_A_key], color='orange', alpha=0.8, linestyle='-', linewidth=1, label=pi_A_key.replace('pi_', ''))
    #    # #ax.plot(window_df['rel_pos'], window_df[pi_B_key], color='dodgerblue', alpha=0.8, linestyle='-', linewidth=1, label=pi_B_key.replace('pi_', ''))
    #    # #y_lim = (min(window_df[pi_A_key].min(), window_df[pi_B_key].min()), max(window_df[pi_A_key].max(), window_df[pi_B_key].max()))
    #    # #ax.vlines(x_boundaries, y_lim[0], y_lim[1], colors=['lightgrey'], linestyles='dashed', linewidth=1)
    #    # #ax.set_ylim(y_lim)
    #    # #ax.spines['right'].set_visible(False)
    #    # #ax.spines['top'].set_visible(False)
    #    # #ax.legend(numpoints=1)
    #    # #plt.ylabel('Pi')
    #    # #plt.xlabel("Genome coordinate")
    #    out_f = '%s.pcp.png' % self.prefix
    #    plt.savefig(out_f, format="png")
        #print("[>] Created: %r" % str(out_f))
        #plt.close(fig)

        #fig, host = plt.subplots()
        #
        ## create some dummy data
        #ynames = ['P1', 'P2', 'P3', 'P4', 'P5']
        #N1, N2, N3 = 10, 5, 8
        #N = N1 + N2 + N3
        #category = np.concatenate([np.full(N1, 1), np.full(N2, 2), np.full(N3, 3)])
        #y1 = np.random.uniform(0, 10, N) + 7 * category
        #y2 = np.sin(np.random.uniform(0, np.pi, N)) ** category
        #y3 = np.random.binomial(300, 1 - category / 10, N)
        #y4 = np.random.binomial(200, (category / 6) ** 1/3, N)
        #y5 = np.random.uniform(0, 800, N)
        #
        ## organize the data
        #ys = np.dstack([y1, y2, y3, y4, y5])[0]
        #ymins = ys.min(axis=0)
        #ymaxs = ys.max(axis=0)
        #dys = ymaxs - ymins
        #ymins -= dys * 0.05  # add 5% padding below and above
        #ymaxs += dys * 0.05
        #dys = ymaxs - ymins
        #
        ## transform all data to be compatible with the main axis
        #zs = np.zeros_like(ys)
        #zs[:, 0] = ys[:, 0]
        #zs[:, 1:] = (ys[:, 1:] - ymins[1:]) / dys[1:] * dys[0] + ymins[0]
        #
        #
        #axes = [host] + [host.twinx() for i in range(ys.shape[1] - 1)]
        #for i, ax in enumerate(axes):
        #    ax.set_ylim(ymins[i], ymaxs[i])
        #    ax.spines['top'].set_visible(False)
        #    ax.spines['bottom'].set_visible(False)
        #    if ax != host:
        #        ax.spines['left'].set_visible(False)
        #        ax.yaxis.set_ticks_position('right')
        #        ax.spines["right"].set_position(("axes", i / (ys.shape[1] - 1)))
        #
        #host.set_xlim(0, ys.shape[1] - 1)
        #host.set_xticks(range(ys.shape[1]))
        #host.set_xticklabels(ynames, fontsize=14)
        #host.tick_params(axis='x', which='major', pad=7)
        #host.spines['right'].set_visible(False)
        #host.xaxis.tick_top()
        #host.set_title('Parallel Coordinates Plot', fontsize=18)
        #
        #colors = plt.cm.tab10.colors
        #for j in range(N):
        #    # to just draw straight lines between the axes:
        #    # host.plot(range(ys.shape[1]), zs[j,:], c=colors[(category[j] - 1) % len(colors) ])
        #
        #    # create bezier curves
        #    # for each axis, there will a control vertex at the point itself, one at 1/3rd towards the previous and one
        #    #   at one third towards the next axis; the first and last axis have one less control vertex
        #    # x-coordinate of the control vertices: at each integer (for the axes) and two inbetween
        #    # y-coordinate: repeat every point three times, except the first and last only twice
        #    verts = list(zip([x for x in np.linspace(0, len(ys) - 1, len(ys) * 3 - 2, endpoint=True)],
        #                     np.repeat(zs[j, :], 3)[1:-1]))
        #    # for x,y in verts: host.plot(x, y, 'go') # to show the control points of the beziers
        #    codes = [Path.MOVETO] + [Path.CURVE4 for _ in range(len(verts) - 1)]
        #    path = Path(verts, codes)
        #    patch = patches.PathPatch(path, facecolor='none', lw=1, edgecolor=colors[category[j] - 1])
        #    host.add_patch(patch)
        #plt.tight_layout()
        #plt.show()

    
#     def plot_pi_genome_scan(self, window_df):
#         offset_by_sequence_id = {}
#         offset = 0
#         x_boundaries = []
#         for sequence_id, sequence_length in zip(self.data.attrs['sequence_ids'], self.data.attrs['sequence_length']):
#             offset_by_sequence_id[sequence_id] = offset
#             x_boundaries.append(offset)
#             offset += sequence_length
#         x_boundaries.append(offset)
#         #print([(sequenceObj.id, sequenceObj.length) for sequenceObj in sequenceObjs])
#         #print(x_boundaries)
#         fig = plt.figure(figsize=(18,4), dpi=200, frameon=True)
#         #connecting dots
#         ax = fig.add_subplot(111)  
#         window_df['rel_pos'] = window_df['midpoint_median'] + window_df['sequence_id'].map(offset_by_sequence_id)
#         window_df.sort_values(['rel_pos'], inplace=True)
#         #print(window_df)
#         pi_A_key = list(window_df.columns)[4]
#         pi_B_key = list(window_df.columns)[5]
#         ax.plot(window_df['rel_pos'], window_df[pi_A_key], color='orange', alpha=0.8, linestyle='-', linewidth=1, label=pi_A_key.replace('pi_', ''))
#         ax.plot(window_df['rel_pos'], window_df[pi_B_key], color='dodgerblue', alpha=0.8, linestyle='-', linewidth=1, label=pi_B_key.replace('pi_', ''))
#         y_lim = (min(window_df[pi_A_key].min(), window_df[pi_B_key].min()), max(window_df[pi_A_key].max(), window_df[pi_B_key].max()))
#         ax.vlines(x_boundaries, y_lim[0], y_lim[1], colors=['lightgrey'], linestyles='dashed', linewidth=1)
#         ax.set_ylim(y_lim)
#         ax.spines['right'].set_visible(False)
#         ax.spines['top'].set_visible(False)
#         ax.legend(numpoints=1)
#         plt.ylabel('Pi')
#         plt.xlabel("Genome coordinate")
#         out_f = '%s.pi_genome_scan.png' % self.prefix
#         plt.tight_layout()
#         fig.savefig(out_f, format="png")
#         print("[>] Created: %r" % str(out_f))
#         plt.close(fig)

#     def plot_fst_genome_scan(self, window_df):
#         offset_by_sequence_id = {}
#         offset = 0
#         x_boundaries = []
#         for sequence_id, sequence_length in zip(self.data.attrs['sequence_ids'], self.data.attrs['sequence_length']):
#             offset_by_sequence_id[sequence_id] = offset
#             x_boundaries.append(offset)
#             offset += sequence_length
#         x_boundaries.append(offset)
#         fig = plt.figure(figsize=(18,4), dpi=200, frameon=True)
#         #connecting dots
#         ax = fig.add_subplot(111)  
#         y_lim = (0.0, 1.0)
#         window_df['rel_pos'] = window_df['midpoint_median'] + window_df['sequence_id'].map(offset_by_sequence_id)
#         window_df.sort_values(['rel_pos'], inplace=True)
#         ax.plot(window_df['rel_pos'], window_df['f_st'], color='lightgrey', alpha=0.8, linestyle='-', linewidth=1)
#         scatter = ax.scatter(window_df['rel_pos'], window_df['f_st'], c=window_df['d_xy'], alpha=1.0, cmap='PiYG_r', edgecolors='white', marker='o', s=40, linewidth=0.2)
#         cbar = fig.colorbar(scatter, ax=ax)
#         cbar.ax.set_title('D_xy')
#         ax.vlines(x_boundaries, 0.0, 1.0, colors=['lightgrey'], linestyles='dashed', linewidth=1)
#         ax.set_ylim(y_lim)
#         ax.spines['right'].set_visible(False)
#         ax.spines['top'].set_visible(False)
#         plt.ylabel('F_st')
#         plt.xlabel("Genome coordinate")
#         ax.autoscale_view(tight=None, scalex=True, scaley=True)
#         out_f = '%s.fst_genome_scan.png' % self.prefix
#         fig.savefig(out_f, format="png")
#         plt.close(fig)
#         print("[>] Created: %r" % str(out_f))

    #def dump_blocks(self, parameterObj, cartesian_only=True):
    #    meta = self.data['seqs'].attrs
    #    sample_set_idxs = [idx for (idx, is_cartesian) in enumerate(meta['sample_sets_inter']) if is_cartesian] if cartesian_only else range(len(meta['sample_sets']))
    #    variation_global = []
    #    with tqdm(total=(len(meta['seq_names']) * len(sample_set_idxs)), desc="[%] Writing bSFSs ", ncols=100, unit_scale=True) as pbar: 
    #        for seq_name in meta['seq_names']: 
    #            for sample_set_idx in sample_set_idxs:
    #                variation_key = 'seqs/%s/blocks/%s/variation' % (seq_name, sample_set_idx)
    #                variation_global.append(np.array(self.data[variation_key]))#[valid]
    #                pbar.update()
    #    variation_global_array = np.concatenate(variation_global, axis=0)
    #    # popgen
    #    variation_global = []
        #metrics_rows = []
        # is order (pi_1, pi_2, d_xy, f_st, fgv) correct?
        # for sample_set_idx in data_by_key_by_sample_set_idx:
        #     sample_set_ids = self.data.attrs['sample_sets'][sample_set_idx]
        #     #print(data_by_key_by_sample_set_idx)
        #     block_sites = np.sum(np.concatenate(data_by_key_by_sample_set_idx[sample_set_idx]['block_sites'], axis=0))
        #     interval_sites = np.sum(np.concatenate(data_by_key_by_sample_set_idx[sample_set_idx]['interval_sites'], axis=0))
        #     block_sites_valid = np.sum(np.concatenate(data_by_key_by_sample_set_idx[sample_set_idx]['block_sites_valid'], axis=0))
        #     variation_array = np.concatenate(data_by_key_by_sample_set_idx[sample_set_idx]['variation'], axis=0)
        #     missing_count = np.sum(np.concatenate(data_by_key_by_sample_set_idx[sample_set_idx]['missing'], axis=0))
        #     multiallelic_count = np.sum(np.concatenate(data_by_key_by_sample_set_idx[sample_set_idx]['multiallelic'], axis=0))
        #     hetB_count, hetA_count, hetAB_count, fixed_count = np.sum(variation_array, axis=0)
        #     #pi_1, pi_2, d_xy, f_st, fgv = calculate_popgen_from_array(variation_array, (self.data.attrs['block_length'] * variation_array.shape[0]))    
        #     pi_1, pi_2, d_xy, f_st, fgv = calculate_popgen_from_array(variation_array, block_sites_valid)    
        #     metrics_rows.append([
        #         sample_set_ids[0], 
        #         sample_set_ids[1],     
        #         block_sites,
        #         interval_sites,
        #         block_sites_valid,
        #         np.divide(block_sites_valid, self.data.attrs['block_length']),
        #         fgv,
        #         missing_count,
        #         multiallelic_count,
        #         hetA_count, 
        #         hetB_count, 
        #         hetAB_count, 
        #         fixed_count,
        #         pi_1, 
        #         pi_2, 
        #         d_xy, 
        #         f_st
        #         ])
        # # output metrics 
        # header = [
        #     self.data.attrs['pop_ids'][0], 
        #     self.data.attrs['pop_ids'][1], 
        #     'block_sites', 
        #     'interval_sites', 
        #     'block_sites_valid', 
        #     'blocks', 
        #     'fgv', 
        #     'missing', 
        #     'multiallelic', 
        #     'hetA', 
        #     'hetB', 
        #     'hetAB', 
        #     'fixed', 
        #     'piA', 
        #     'piB', 
        #     'dxy', 
        #     'fst'
        #     ]
        # pd.DataFrame(data=metrics_rows, columns=header, dtype='int64').to_hdf("%s.block_stats.h5" % self.prefix, 'bsfs', format='table')

        #pi_1, pi_2, d_xy, f_st, fgv = calculate_popgen_from_array(variation_global_array, (self.data.attrs['block_length'] * variation_global_array.shape[0]))
        #print("[+] Pi_%s = %s; Pi_%s = %s; D_xy = %s; F_st = %s; FGVs = %s / %s blocks (%s)" % (self.data.attrs['pop_ids'][0], pi_1, self.data.attrs['pop_ids'][1], pi_2, d_xy, f_st, fgv, variation_global_array.shape[0], format_percentage(fgv / variation_global_array.shape[0]))) 


#    def test_dask(self, grids=None, data=None):
#        if grids is None or data is None:
#            raise ValueError('gridsearch: needs grid and data') 
#        from dask.distributed import LocalCluster, Client
#        cluster= LocalCluster(
#            n_workers=8, 
#            threads_per_worker=1,
#            dashboard_address='localhost:8787', 
#            interface='lo0', 
#            **{'local_directory': 'dasktemp', 'memory_limit': '2G'})
#        client = Client(cluster)
#        array = dask.array.ones((100000,100000), chunks=(10000,10000))
#        a = client.submit(dask.array.mean, array).result()
#        return a.compute()
        
#   def gridsearch(self, grids=None, data=None):
#       '''returns 2d array of likelihoods of shape (windows, grids)'''
#       if grids is None or data is None:
#           raise ValueError('gridsearch: needs grid and data') 
#       from dask.distributed import Client, LocalCluster
#       cluster= LocalCluster(
#           n_workers=8, 
#           threads_per_worker=1,
#           dashboard_address='localhost:8787', 
#           interface='lo0', 
#           **{'local_directory': 'dasktemp', 'memory_limit': '3G'})
#       client = Client(cluster)
#       data_array = dask.array.from_array(data, chunks=(10000, *data.shape[1:]))
#       print('# data_array', type(data_array))
#       grid_array = dask.array.from_array(grids, chunks=(1, *grids.shape[1:]))
#       print('# grid_array', type(grid_array))
#       grids_masked = dask.array.ma.masked_array(grids, grids==0, fill_value=np.nan)
#       grids_log_temp = client.submit(dask.array.log, grids_masked).result()

#       print('# grids_log_temp', type(grids_log_temp))
#       grids_log = client.submit(dask.array.nan_to_num, grids_log_temp).result().compute()
#       print('grids_log', type(grids_log))
#       print('done')
#       # print('data', type(data))
#       # print('grids', type(grids))
#       # data = dask.array.from_array(data, chunks=(500, *data.shape[1:]))
#       # # print('data', type(data), data.chunks)
#       # grids = dask.array.from_array(grids, chunks=(10, *grids.shape[1:]))
#       # # print('grids', type(grids), grids.chunks)
#       # #np.log(grids, where=grids>0, out=grids_log)
#       # grids[grids==0] = np.nan
#       # 
#       # grids_log = client.submit(dask.array.log, grids).result().compute()
#       # print('grids_log', type(grids_log))
#       # grids_log[grids_log==np.nan] = 0
#       # data_scattered = client.scatter(data[:, None])
#       # grids_log_scattered = client.scatter(grids_log)
#       # m_res = client.submit(dask.array.multiply, data_scattered, grids_log_scattered)
#       # #m_res.rechunk({0:100, 1: 4, 2: 4, 3: 4, 4: 4})
#       # #res_scattered = client.scatter(m_res)
#       # #r_res = client.submit(dask.array.apply_over_axes, dask.array.sum, res_scattered, axes=[2,3,4,5])
#       # x_res = client.submit(dask.array.apply_over_axes, dask.array.sum, m_res, axes=[2,3,4,5])
#       # #m1_res.rechunk(100000)
#       # #r_res_scattered = client.scatter(r_res)
#       # y_res = client.submit(dask.array.squeeze, x_res)
#       # y_res.result().compute()
#       # #res.visualize()
#       # print('y_res', type(y_res))
#       # return y_res
#       #return a
#       return True

    #def _process_config(self):
#
    #    if self._MODULE in ['makegrid', 'inference', 'simulate', 'gridsearch']:
    #        self.config['mu']['blockslength'] = self._get_blocks_length(self.zstore)
    #        self.config['parameters']['mu'] = self.config['mu']['mu']
    #        self._expand_params()
    #        self.reference, self.toBeSynced = self._get_pops_to_sync()
    #        self._remove_pop_from_dict(self.toBeSynced)
    #        self.parameter_combinations = self._dict_product()
    #        self._sync_pop_sizes(self.reference, self.toBeSynced)
    #    elif self._MODULE in ['optimize', 'optimize']:
    #        #TO BE CHECKED: which bits are we still using
    #        #determine parameters that are fixed:
    #        self.fixed_params = self._get_fixed_params()
    #        #self.config['mu']['blockslength'] = self._get_blocks_length(self.zstore)
    #        #self.config['parameters']['mu'] = self.config['mu']['mu']
    #        reference_pop=self.config['populations']['reference_pop']
    #        #syncing pop sizes
    #        self.reference, self.toBeSynced = self._get_pops_to_sync()
    #        if self.toBeSynced:
    #            if reference_pop in self.toBeSynced:
    #                sys.exit(f"[X] Set reference pop to {self.reference}.")
    #        toBeSynced_pops = [f'Ne_{s}' for s in self.toBeSynced] if self.toBeSynced!=None else []
    #        self.fixed_params = [pop for pop in self.fixed_params if pop not in toBeSynced_pops]
    #        #verify if any Ne fixed, whether one of those Ne is self.reference
    #        fixed_Nes = [p for p in self.fixed_params if p.startswith('Ne')]
    #        if len(fixed_Nes)>0:
    #            if not f"Ne_{reference_pop}" in fixed_Nes:
    #                sys.exit("[X] No. No. No. It would make much more sense to set a population with a fixed size as reference.")
    #        #self._sync_pop_sizes_optimize(self.reference, self.toBeSynced)
    #        self.parameter_combinations = self._return_boundaries()
    #    else:
    #        sys.exit("[X] gimble.py_processing_config: Not implemented yet.")