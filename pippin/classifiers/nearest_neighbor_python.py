import inspect
import os
import shutil
import subprocess
from pathlib import Path

from pippin.classifiers.classifier import Classifier
from pippin.config import get_config, get_output_loc, mkdirs, merge_dict, get_data_loc
from pippin.task import Task


class NearestNeighborPyClassifier(Classifier):
    """ Nearest Neighbor Python classifier

    CONFIGURATION:
    ==============
    CLASSIFICATION:
      label:
        MASK: TEST  # partial match on sim and classifier
        MASK_SIM: TEST  # partial match on sim name
        MASK_FIT: TEST  # partial match on lcfit name
        MODE: train/predict
        OPTS:
          FITOPT: someLabel # Exact match to fitopt in a fitopt file. USED FOR TRAINING ONLY
          FEATURES: x1 c zHD  # Columns out of fitres file to use as features
          MODEL: someName # exact name of training classification task

    OUTPUTS:
    ========
        name : name given in the yml
        output_dir: top level output directory
        prob_column_name: name of the column to get probabilities out of
        predictions_filename: location of csv filename with id/probs

    """

    def __init__(self, name, output_dir, config, dependencies, mode, options, index=0, model_name=None):
        super().__init__(name, output_dir, config, dependencies, mode, options, index=index, model_name=model_name)
        self.global_config = get_config()
        self.num_jobs = 1

        self.conda_env = self.global_config["SNIRF"]["conda_env"]

        self.path_to_classifier = os.path.dirname(inspect.stack()[0][1])
        self.job_base_name = os.path.basename(Path(output_dir).parents[1]) + "__" + os.path.basename(output_dir)
        self.features = options.get("FEATURES", "zHD x1 c cERR x1ERR COV_x1_c COV_x1_x0 COV_c_x0 PKMJDERR")
        # self.model_pk_file = self.get_unique_name() + ".pkl"
        self.model_pk_file = "model.pkl"

        self.output_pk_file = os.path.join(self.output_dir, self.model_pk_file)
        self.predictions_filename = os.path.join(self.output_dir, "predictions.csv")

        self.fitopt = options.get("FITOPT", "DEFAULT")

        self.batch_file = self.options.get("BATCH_FILE")
        if self.batch_file is not None:
            self.batch_file = get_data_loc(self.batch_file)
        self.batch_replace = self.options.get("BATCH_REPLACE", {})

        self.output["predictions_filename"] = self.predictions_filename
        self.output["model_filename"] = self.output_pk_file
        self.validate_model()

        self.slurm = """{sbatch_header}
        {task_setup}

if [ $? -ne 0 ]; then
    echo FAILURE > {done_file}
fi
"""

    def setup(self):
        lcfit = self.get_fit_dependency()
        self.fitres_filename = lcfit["fitopt_map"][self.fitopt]
        self.fitres_file = os.path.abspath(os.path.join(lcfit["fitres_dirs"][self.index], self.fitres_filename))

    def classify(self, command):
        self.setup()
        if self.batch_file is None:
            if self.gpu:
                self.sbatch_header = self.sbatch_gpu_header
            else:
                self.sbatch_header = self.sbatch_cpu_header
        else:
            with open(self.batch_file, 'r') as f:
                self.sbatch_header = f.read()
            self.sbatch_header = self.clean_header(self.sbatch_header)

        header_dict = {
                "REPLACE_NAME": self.job_base_name,
                "REPLACE_LOGFILE": "output.log",
                "REPLACE_WALLTIME": "00:55:00",
                "REPLACE_MEM": "8GB",
                "APPEND": ["#SBATCH --ntasks=1", "#SBATCH --cpus-per-task=4"]
                }
        header_dict = merge_dict(header_dict, self.batch_replace)
        self.update_header(header_dict)

        setup_dict = {
            "job_name": self.job_base_name,
            "conda_env": self.conda_env,
            "path_to_classifier": self.path_to_classifier,
            "command_opts": command
        }

        format_dict = {
                "done_file": self.done_file,
                "sbatch_header": self.sbatch_header,
                "task_setup": self.update_setup(setup_dict, self.task_setup['nearest_neighbour'])
                }

        slurm_script = self.slurm.format(**format_dict)

        new_hash = self.get_hash_from_string(slurm_script)
        if self._check_regenerate(new_hash):
            self.logger.debug("Regenerating")
            shutil.rmtree(self.output_dir, ignore_errors=True)
            mkdirs(self.output_dir)

            slurm_output_file = self.output_dir + "/job.slurm"
            with open(slurm_output_file, "w") as f:
                f.write(slurm_script)
            self.save_new_hash(new_hash)
            self.logger.info(f"Submitting batch job {slurm_output_file}")
            subprocess.run(["sbatch", slurm_output_file], cwd=self.output_dir)
        else:
            self.logger.info("Hash check passed, not rerunning")
            self.should_be_done()
        return True

    def predict(self):
        self.setup()
        model = self.options.get("MODEL")
        if model is None:
            self.logger.error("If you are in predict model, please specify a MODEL in OPTS. Either a file location or a training task name.")
            return False
        if not os.path.exists(get_output_loc(model)):
            # If its not a file, it must be a task
            for t in self.dependencies:
                if model == t.name:
                    self.logger.debug(f"Found task dependency {t.name} with model file {t.output['model_filename']}")
                    model = t.output["model_filename"]
        else:
            model = get_output_loc(model)
        types = " ".join([str(a) for a in self.get_simulation_dependency().output["types_dict"]["IA"]])
        if not types:
            types = "1"
        command = (
            f"-p "
            f"--features {self.features} "
            f"--done_file {self.done_file} "
            f"--model {model} "
            f"--types {types} "
            f"--name {self.get_prob_column_name()} "
            f"--output {self.predictions_filename} "
            f"{self.fitres_file}"
        )
        return self.classify(command)

    def train(self):
        types = " ".join([str(a) for a in self.get_simulation_dependency().output["types_dict"]["IA"]])
        if not types:
            self.logger.error("No Ia types for a training sim!")
            return False
        command = (
            f"--features {self.features} "
            f"--done_file {self.done_file} "
            f"--model {self.output_pk_file} "
            f"--types {types} "
            f"--name {self.get_prob_column_name()} "
            f"--output {self.predictions_filename} "
            f"{self.fitres_file}"
        )
        return self.classify(command)

    def _check_completion(self, squeue):
        if os.path.exists(self.done_file):
            self.logger.debug(f"Found done file at {self.done_file}")
            with open(self.done_file) as f:
                if "FAILURE" in f.read().upper():
                    return Task.FINISHED_FAILURE
                return Task.FINISHED_SUCCESS
        return self.check_for_job(squeue, self.job_base_name)

    @staticmethod
    def get_requirements(options):
        return False, True
