import os
from abc import abstractmethod

from pippin.config import get_output_loc, get_data_loc
from pippin.dataprep import DataPrep
from pippin.task import Task
from pippin.snana_sim import SNANASimulation
from pippin.snana_fit import SNANALightCurveFit


class Classifier(Task):
    """ Classification task

    CONFIGURATION:
    ==============
    CLASSIFICATION:
      label:
        MASK: TEST  # partial match on sim and classifier
        MASK_SIM: TEST  # partial match on sim name
        MASK_FIT: TEST  # partial match on lcfit name
        MODE: train/predict # Some classifiers dont need training and so you can set to predict straight away
        OPTS:
          CHANGES_FOR_INDIVIDUAL_CLASSIFIERS

    OUTPUTS:
    ========
        name : name given in the yml
        output_dir: top level output directory
        prob_column_name: name of the column to get probabilities out of
        predictions_filename: location of csv filename with id/probs

    """

    TRAIN = 0
    PREDICT = 1

    def __init__(self, name, output_dir, config, dependencies, mode, options, index=0, model_name=None):
        super().__init__(name, output_dir, config=config, dependencies=dependencies)
        self.options = options
        self.index = index
        self.mode = mode
        self.model_name = model_name
        self.output["prob_column_name"] = self.get_prob_column_name()
        self.output["index"] = index

    @abstractmethod
    def predict(self):
        """ Predict probabilities for given dependencies

        :return: true or false for success in launching the job
        """
        pass

    @abstractmethod
    def train(self):
        """ Train a model to file for given dependencies

        :return: true or false for success in launching the job
        """
        pass

    @staticmethod
    def get_requirements(config):
        """ Return what data is actively used by the classifier

        :param config: the input dictionary `OPTS` from the config file
        :return: a two tuple - (needs simulation photometry, needs a fitres file)
        """
        return True, True

    def get_fit_dependency(self, output=True):
        for t in self.dependencies:
            if isinstance(t, SNANALightCurveFit):
                return t.output if output else t
        return None

    def get_simulation_dependency(self):
        for t in self.dependencies:
            if isinstance(t, SNANASimulation) or isinstance(t, DataPrep):
                return t
        for t in self.get_fit_dependency(output=False).dependencies:
            if isinstance(t, SNANASimulation) or isinstance(t, DataPrep):
                return t
        return None

    def validate_model(self):
        if self.mode == Classifier.PREDICT:
            model = self.options.get("MODEL")
            if model is None:
                Task.fail_config(f"Classifier {self.name} is in predict mode but does not have a model specified")
            model_classifier = self.get_model_classifier()
            if model_classifier is not None and model_classifier.name == model:
                return True
            path = get_data_loc(model)
            if not os.path.exists(path):
                Task.fail_config(f"Classifier {self.name} does not have a classifier dependency and model is not a serialised file path")
        return True

    def get_model_classifier(self):
        for t in self.dependencies:
            if isinstance(t, Classifier):
                return t
        return None

    def _run(self):
        if self.mode == Classifier.TRAIN:
            return self.train()
        elif self.mode == Classifier.PREDICT:
            return self.predict()

    def get_unique_name(self):
        name = self.name
        use_sim, use_fit = self.get_requirements(self.options)
        if use_fit:
            name += "_" + self.get_fit_dependency()["name"]
        else:
            name += "_" + self.get_simulation_dependency().output["name"]
        return name

    def get_prob_column_name(self):
        m = self.get_model_classifier()
        if m is None:
            if self.model_name is not None:
                return f"PROB_{self.model_name}"
            else:
                return f"PROB_{self.get_unique_name()}"
        else:
            return m.output["prob_column_name"]

    @staticmethod
    def get_tasks(c, prior_tasks, base_output_dir, stage_number, prefix, global_config):
        from pippin.classifiers.factory import ClassifierFactory

        def _get_clas_output_dir(base_output_dir, stage_number, sim_name, fit_name, clas_name, index=None, extra=None):
            sim_name = "" if sim_name is None or fit_name is not None else "_" + sim_name
            fit_name = "" if fit_name is None else "_" + fit_name
            extra_name = "" if extra is None else "_" + extra
            index = "" if index is None else f"_{index}"
            return f"{base_output_dir}/{stage_number}_CLAS/{clas_name}{index}{sim_name}{fit_name}{extra_name}"

        def get_num_ranseed(sim_task, lcfit_task):
            if sim_task is not None:
                return len(sim_task.output["sim_folders"])
            if lcfit_task is not None:
                return len(lcfit_task.output["fitres_dirs"])
            raise ValueError("Classifier dependency has no sim_task or lcfit_task?")

        tasks = []
        lcfit_tasks = Task.get_task_of_type(prior_tasks, SNANALightCurveFit)
        sim_tasks = Task.get_task_of_type(prior_tasks, DataPrep, SNANASimulation)
        for clas_name in c.get("CLASSIFICATION", []):
            config = c["CLASSIFICATION"][clas_name]
            name = config["CLASSIFIER"]
            cls = ClassifierFactory.get(name)
            options = config.get("OPTS", {})
            if "MODE" not in config:
                Task.fail_config(f"Classifier task {clas_name} needs to specify MODE as train or predict")
            mode = config["MODE"].lower()
            assert mode in ["train", "predict"], "MODE should be either train or predict"
            if mode == "train":
                mode = Classifier.TRAIN
            else:
                mode = Classifier.PREDICT

            # Validate that train is not used on certain classifiers
            if mode == Classifier.TRAIN:
                assert name not in ["PerfectClassifier", "UnityClassifier", "FitProbClassifier"], f"Can not use train mode with {name}"

            needs_sim, needs_lc = cls.get_requirements(options)

            runs = []
            if needs_sim and needs_lc:
                runs = [(l.dependencies[0], l) for l in lcfit_tasks]
            elif needs_sim:
                runs = [(s, None) for s in sim_tasks]
            elif needs_lc:
                runs = [(l.dependencies[0], l) for l in lcfit_tasks]
            else:
                Task.logger.warn(f"Classifier {name} does not need sims or fits. Wat.")

            num_gen = 0
            mask = config.get("MASK", "")
            mask_sim = config.get("MASK_SIM", "")
            mask_fit = config.get("MASK_FIT", "")
            for s, l in runs:

                sim_name = s.name if s is not None else None
                fit_name = l.name if l is not None else None
                matched_sim = True
                matched_fit = True
                if mask:
                    matched_sim = matched_sim and mask in sim_name
                if mask_sim:
                    matched_sim = matched_sim and mask_sim in sim_name
                if mask:
                    matched_fit = matched_fit and mask in sim_name
                if mask_fit:
                    matched_fit = matched_fit and mask_sim in sim_name
                if not matched_fit or not matched_sim:
                    continue
                deps = []
                if s is not None:
                    deps.append(s)
                if l is not None:
                    deps.append(l)

                model = options.get("MODEL")

                # Validate to make sure training samples only have one sim.
                if mode == Classifier.TRAIN:
                    if s is not None:
                        folders = s.output["sim_folders"]
                        assert (
                            len(folders) == 1
                        ), f"Training requires one version of the sim, you have {len(folders)} for sim task {s}. Make sure your training sim doesn't set RANSEED_CHANGE"
                    if l is not None:
                        folders = l.output["fitres_dirs"]
                        assert (
                            len(folders) == 1
                        ), f"Training requires one version of the lcfits, you have {len(folders)} for lcfit task {l}. Make sure your training sim doesn't set RANSEED_CHANGE"
                if model is not None:
                    if "/" in model or "." in model:
                        potential_path = get_output_loc(model)
                        if os.path.exists(potential_path):
                            extra = os.path.basename(os.path.dirname(potential_path))

                            # Nasty duplicate code, TODO fix this
                            indexes = get_num_ranseed(s, l)
                            for i in range(indexes):
                                num = i + 1 if indexes > 1 else None
                                clas_output_dir = _get_clas_output_dir(base_output_dir, stage_number, sim_name, fit_name, clas_name, index=num, extra=extra)
                                cc = cls(clas_name, clas_output_dir, config, deps, mode, options, index=i, model_name=extra)
                                Task.logger.info(
                                    f"Creating classification task {name} with {cc.num_jobs} jobs, for LC fit {fit_name} on simulation {sim_name} and index {i}"
                                )
                                num_gen += 1
                                tasks.append(cc)

                        else:
                            Task.fail_config(f"Your model {model} looks like a path, but I couldn't find a model at {potential_path}")
                    else:
                        for t in tasks:
                            if model == t.name:
                                # deps.append(t)
                                extra = t.get_unique_name()

                                assert t.__class__ == cls, f"Model {clas_name} with class {cls} has model {model} with class {t.__class__}, they should match!"

                                indexes = get_num_ranseed(s, l)
                                for i in range(indexes):
                                    num = i + 1 if indexes > 1 else None
                                    clas_output_dir = _get_clas_output_dir(base_output_dir, stage_number, sim_name, fit_name, clas_name, index=num, extra=extra)
                                    cc = cls(clas_name, clas_output_dir, config, deps + [t], mode, options, index=i)
                                    Task.logger.info(
                                        f"Creating classification task {name} with {cc.num_jobs} jobs, for LC fit {fit_name} on simulation {sim_name} and index {i}"
                                    )
                                    num_gen += 1
                                    tasks.append(cc)
                else:

                    indexes = get_num_ranseed(s, l)
                    for i in range(indexes):
                        num = i + 1 if indexes > 1 else None
                        clas_output_dir = _get_clas_output_dir(base_output_dir, stage_number, sim_name, fit_name, clas_name, index=num)
                        cc = cls(clas_name, clas_output_dir, config, deps, mode, options, index=i)
                        Task.logger.info(
                            f"Creating classification task {name} with {cc.num_jobs} jobs, for LC fit {fit_name} on simulation {sim_name} and index {i}"
                        )
                        num_gen += 1
                        tasks.append(cc)

            if num_gen == 0:
                Task.fail_config(f"Classifier {clas_name} with masks |{mask}|{mask_sim}|{mask_fit}| matched no combination of sims and fits")
        return tasks
