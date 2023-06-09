import logging
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from functools import partial
from glob import iglob
from pathlib import Path
from typing import Iterable, Optional, Union

import pandas as pd

from elbow.extractors import Extractor
from elbow.filters import FileModifiedIndex, hash_partitioner
from elbow.pipeline import Pipeline
from elbow.record import RecordBatch
from elbow.sinks import BufferedParquetWriter
from elbow.typing import StrOrPath
from elbow.utils import atomicopen, cpu_count, setup_logging


def build_table(
    source: Union[str, Iterable[StrOrPath]],
    extract: Extractor,
    *,
    max_failures: Optional[int] = 0,
) -> pd.DataFrame:
    """
    Extract records from a stream of files and load into a pandas DataFrame

    Args:
        source: shell-style file pattern as in `glob.glob()` or iterable of paths.
            Patterns containing '**' will match any files and zero or more directories
        extract: extract function mapping file paths to records
        max_failures: number of failures to tolerate

    Returns:
        A DataFrame containing the concatenated records (in arbitrary order)
    """
    if isinstance(source, str):
        source = iglob(source, recursive=True)

    batch = RecordBatch()
    pipe = Pipeline(
        source=source, extract=extract, sink=batch.append, max_failures=max_failures
    )
    pipe.run()

    df = batch.to_df()
    return df


def build_parquet(
    source: Union[str, Iterable[StrOrPath]],
    extract: Extractor,
    output: StrOrPath,
    *,
    incremental: bool = False,
    overwrite: bool = False,
    workers: Optional[int] = None,
    worker_id: Optional[int] = None,
    max_failures: Optional[int] = 0,
) -> None:
    """
    Extract records from a stream of files and save as a Parquet dataset

    Args:
        source: shell-style file pattern as in `glob.glob()` or iterable of paths.
            Patterns containing '**' will match any files and zero or more directories
        extract: extract function mapping file paths to records
        output: path to output parquet dataset directory
        incremental: update dataset incrementally with only new or changed files.
        overwrite: overwrite previous results.
        workers: number of parallel processes. If `None` or 1, run in the main
            process. Setting to -1 runs as many processes as there are cores available.
        worker_id: optional worker ID to use when scheduling parallel tasks externally.
            Specifying the number of workers is required in this case. Incompatible with
            overwrite.
        max_failures: number of extract failures to tolerate
    """
    # TODO:
    #     - generalize sources
    #     - parallel extraction is a bit awkward due to hashing assignment might consider
    #       pre-expanding the sources and partitioning. But this is susceptible to racing.
    if workers is None:
        workers = 1
    elif workers == -1:
        workers = cpu_count()
    elif workers <= 0:
        raise ValueError(f"Invalid workers {workers}; expected -1 or > 0")

    if worker_id is not None:
        if not 0 <= worker_id < workers:
            raise ValueError(
                f"Invalid worker_id {worker_id}; expeced 0 <= worker_id < {workers}"
            )
        if overwrite:
            raise ValueError("Can't overwrite when using worker_id")

    inplace = incremental or worker_id is not None
    if Path(output).exists() and not inplace:
        if overwrite:
            shutil.rmtree(output)
        else:
            raise FileExistsError(f"Parquet output directory {output} already exists")

    _worker = partial(
        _build_parquet_worker,
        source=source,
        extract=extract,
        output=output,
        incremental=incremental,
        workers=workers,
        max_failures=max_failures,
        log_level=logging.getLogger().level,
    )

    if worker_id is None and workers > 1:
        with ProcessPoolExecutor(workers) as pool:
            futures_to_id = {pool.submit(_worker, ii): ii for ii in range(workers)}

            for future in as_completed(futures_to_id):
                try:
                    future.result()
                except Exception as exc:
                    worker_id = futures_to_id[future]
                    logging.warning(
                        "Generated exception in worker %d", worker_id, exc_info=exc
                    )
    elif worker_id is not None:
        _worker(worker_id)
    else:
        _worker(0)


def _build_parquet_worker(
    worker_id: int,
    *,
    source: Union[str, Iterable[StrOrPath]],
    extract: Extractor,
    output: StrOrPath,
    incremental: bool,
    workers: int,
    max_failures: Optional[int],
    log_level: int,
):
    setup_logging(log_level)

    start = datetime.now()
    output = Path(output)
    if isinstance(source, str):
        source = iglob(source, recursive=True)

    if incremental and output.exists():
        # NOTE: Race to read index while other workers try to write.
        # But it shouldn't matter since each worker gets a unique partition (?).
        file_mod_index = FileModifiedIndex.from_parquet(output)
        source = filter(file_mod_index, source)

    # TODO: maybe let user specify partition key function? By default we will get
    # random assignment of paths to workers.
    if workers > 1:
        partitioner = hash_partitioner(worker_id, workers)
        source = filter(partitioner, source)

    # Include start time in file name in case of multiple incremental loads.
    start_fmt = start.strftime("%Y%m%d%H%M%S")
    output = output / f"part-{start_fmt}-{worker_id:04d}-of-{workers:04d}.parquet"
    if output.exists():
        raise FileExistsError(f"Partition {output} already exists")
    output.parent.mkdir(parents=True, exist_ok=True)

    # Using atomicopen to avoid partial output files and empty file errors.
    with atomicopen(output, "wb") as f:
        with BufferedParquetWriter(where=f) as writer:
            # TODO: should this just be a function?
            pipe = Pipeline(
                source=source, extract=extract, sink=writer, max_failures=max_failures
            )
            counts = pipe.run()

    return counts
