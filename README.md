gIMble
=========

[![DOI](https://zenodo.org/badge/176883840.svg)](https://zenodo.org/badge/latestdoi/176883840)

Dependencies (via [conda](https://conda.io/miniconda.html))
-------

```
# clone repository
git clone https://github.com/DRL/gimble.git

# create conda enviroment with dependencies
conda create -n gimble && \
source activate gimble && \
conda install bedtools bcftools samtools vcflib mosdepth pysam numpy docopt tqdm pandas tabulate zarr nlopt scikit-allel parallel more-itertools networkx giac sagelib matplotlib msprime networkx pygraphviz nlopt sympy cerberus maxima -c conda-forge -c bioconda 
```

Usage
-----

```
Usage: gimble <module> [<args>...] [-D -V -h]

  [Modules]
    preprocess            Preprocess input files
    setup                 Setup data store
    info                  Print information about DataStore
    blocks                Generate blocks from data in DataStore 
    windows               Generate windows from blocks in DataStore (requires blocks)
    query                 Query BED file of blocks (windows [TBI])
    model                 Build demographic model
    simulate              Simulate data [TBI] 
    makegrid              Make grid [TBI]
    gridsearch            Search grid [TBI]
    inference             Make inference [TBI] (requires blocks)
    
    partitioncds          Partition CDS sites in BED file by degeneracy in sample GTs 
    plotbed               Plot BED file [TBR]
```
 
[0] preprocess
--------------

A. generates **genome file** (sequence_id, length) based on FASTA file

B. generates **sample file** (sample_id) based on ReadGroupIDs in BAM files

C. generates **coverage threshold report** for each BAM file

D. processes **VCF file**

+ decomposition of MNPs into SNPs
+ `{RAW_VARIANTS}` = all variants in VCF
+ `{NONSNP}`: non-SNP variants 
+ `{SNPGAP}`: all variants within +/- X b of {NONSNP} variants
+ `{BALANCE}`: all variabts with any un-balanced allele observation (`-e 'RPL<1 | RPR<1 | SAF<1 | SAR<1'`) 
+ `{FAIL} = {{NONSNP} U {SNPGAP} U {BALANCE}} 
+ `{VARIANTS} = {RAW_VARIANTS} - {FAIL}`
+ sample genotypes in `{VARIANTS}` with read depths outside of coverage thresholds are set to missing (`./.`)
    
E. processes **BAM files**:

+ `{RAW_INVARIANT}` = union of all sites with read depths within coverage thresholds in their respective sample (bedtools multiinter)
+ `{INVARIANTS} = {SITES} - {RAW_INVARIANT}`
    
F. logs all executed commands

```
~/gIMble/gIMble preprocess -f FASTA -b BAM_DIR/ -v RAW.vcf.gz -k

# output files 
`gimble.samples.csv`            # A)
`gimble.genomefile`             # B)
`gimble.coverage_summary.csv`   # C)
`gimble.vcf.gz`                 # D)
`gimble.bed`                    # E)
`gimble.log.txt`                # F)
```

[1] Modify input files
--------------

+ `gimble.genomefile`:
    + [OPTIONAL] remove sequence IDs to ignore them in the analyses
+ `gimble.samples.csv` 
    + [REQUIRED] add population IDs to the sample IDs (must be exactly 2)
    + [OPTIONAL] remove sample IDs to ignore them in the analyses
+ `gimble.bed`
    + [RECOMMENDED] intersect with BED regions of interest to analyse particular genomic regions

    e.g `bedtools intersect -a gimble.bed -b my_intergenic_regions.bed > gimble.intergenic.bed` 

[2] Setup
--------------

+ will extract input data into DataStore 

```
./gimble setup -v gimble.vcf.gz -b gimble.intergenic.bed -g gimble.genomefile -s gimble.samples.csv -o analysis
```

[3] Blocks
--------------

+ infers bSFs for a given block length `'-l'` 
+ block span (`end - start`) can be adjusted (default is `2 * '-l'`)

```
./gimble blocks -z analysis.z -l 64
```

[4] Windows 
--------------

+ constructs windows of blocks along the genome

```
./gimble windows -z analysis.z -w 500 -s 100 -z analysis.z
```
